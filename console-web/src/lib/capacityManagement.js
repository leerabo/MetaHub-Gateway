import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LINK = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const CODE = /^[a-z0-9][a-z0-9-]{1,63}$/u;
const REGION = /^[a-z0-9][a-z0-9-]{2,39}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const OBJECT_ID = /^(?:cfc|ltr|che|pbl)_[0-9a-f]{32}$/u;
const JOB_ID = /^pjob_[0-9a-f]{32}$/u;
const SERVICES = new Set(["gateway", "control", "status"]);
const ENVIRONMENTS = new Set(["dev", "staging", "production"]);

export function getPlatformCapacity({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/capacity", { signal }, validCapacityResponse);
}

export function getPlatformUnitEconomics({
  controlApiUrl, billingPeriod = "", currency = "", signal,
} = {}) {
  const query = new URLSearchParams();
  if (billingPeriod) query.set("billing_period", String(billingPeriod));
  if (currency) query.set("currency", String(currency));
  const suffix = query.size ? `?${query}` : "";
  return request(
    controlApiUrl,
    `/platform/unit-economics${suffix}`,
    { signal },
    validEconomicsResponse,
  );
}

export function createCapacityForecast(input = {}) {
  const payload = {
    service_code: String(input.serviceCode ?? ""),
    region_code: String(input.regionCode ?? ""),
    horizon_days: Number(input.horizonDays),
    model_code: "linear-v1",
    confirmed: input.confirmed,
  };
  if (!SERVICES.has(payload.service_code) || !REGION.test(payload.region_code)
    || !new Set([30, 90]).has(payload.horizon_days) || payload.confirmed !== true
    || !integer(input.expectedRevision, 1)) invalid();
  return write(
    input.controlApiUrl,
    "/platform/capacity/forecasts",
    payload,
    input,
    validForecastResponse,
    Number(input.expectedRevision),
  );
}

export function createCapacityLoadTest(input = {}) {
  const payload = {
    scenario_code: String(input.scenarioCode ?? ""),
    environment_class: String(input.environmentClass ?? ""),
    region_code: String(input.regionCode ?? ""),
    synthetic_tenant_count: Number(input.syntheticTenantCount),
    target_rps: Number(input.targetRps),
    duration_seconds: Number(input.durationSeconds),
    confirmed: input.confirmed,
  };
  if (!CODE.test(payload.scenario_code) || !ENVIRONMENTS.has(payload.environment_class)
    || !REGION.test(payload.region_code) || !integer(payload.synthetic_tenant_count, 1, 500)
    || !integer(payload.target_rps, 1, 100000)
    || !integer(payload.duration_seconds, 10, 259200) || payload.confirmed !== true) invalid();
  return write(
    input.controlApiUrl,
    "/platform/load-tests",
    payload,
    input,
    validLoadResponse,
  );
}

export function commandCapacityChaos(input = {}) {
  const action = String(input.action ?? "");
  let payload;
  if (action === "create") {
    payload = {
      action,
      fault_code: String(input.faultCode ?? ""),
      service_code: String(input.serviceCode ?? ""),
      environment_class: String(input.environmentClass ?? ""),
      region_code: String(input.regionCode ?? ""),
      blast_tenant_count: Number(input.blastTenantCount),
      duration_seconds: Number(input.durationSeconds),
      objective_p95_ms: Number(input.objectiveP95Ms),
      objective_error_rate_bps: Number(input.objectiveErrorRateBps),
      stop_error_rate_bps: Number(input.stopErrorRateBps),
      reason_code: String(input.reasonCode ?? ""),
      confirmed: input.confirmed,
    };
    if (!CODE.test(payload.fault_code)
      || !new Set([...SERVICES, "shared"]).has(payload.service_code)
      || !new Set(["staging", "production"]).has(payload.environment_class)
      || !REGION.test(payload.region_code) || !integer(payload.blast_tenant_count, 1, 100)
      || !integer(payload.duration_seconds, 10, 3600)
      || !integer(payload.objective_p95_ms, 1, 60000)
      || !integer(payload.objective_error_rate_bps, 0, 10000)
      || !integer(payload.stop_error_rate_bps, payload.objective_error_rate_bps, 10000)
      || !new Set(["resilience_validation", "release_validation"]).has(payload.reason_code)) {
      invalid();
    }
  } else {
    payload = {
      action,
      chaos_experiment_id: String(input.experimentId ?? ""),
      reason_code: String(input.reasonCode ?? ""),
      confirmed: input.confirmed,
    };
    const allowedReason = action === "approve"
      ? new Set(["release_validation"])
      : action === "kill" ? new Set(["operator_containment", "slo_containment"]) : new Set();
    if (!/^che_[0-9a-f]{32}$/u.test(payload.chaos_experiment_id)
      || !allowedReason.has(payload.reason_code) || !integer(input.revision, 1)) invalid();
  }
  if (payload.confirmed !== true) invalid();
  return write(
    input.controlApiUrl,
    "/platform/chaos-experiments",
    payload,
    input,
    validChaosResponse,
    action === "create" ? null : Number(input.revision),
  );
}

function write(controlApiUrl, path, payload, input, validator, revision = null) {
  if (!IDEMPOTENCY.test(String(input.idempotencyKey ?? ""))) invalid();
  return request(controlApiUrl, path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readPlatformCsrfCookie(),
      "Idempotency-Key": String(input.idempotencyKey),
      ...(revision === null ? {} : { "If-Match": `"${revision}"` }),
    },
    body: JSON.stringify(payload),
    signal: input.signal,
  }, validator);
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
      redirect: "error",
      signal: options?.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError("capacity.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw clientError(
      typeof payload.code === "string" ? payload.code : "capacity.unavailable",
      response.status,
      LINK.test(String(payload.request_id ?? "")) ? payload.request_id : "-",
    );
  }
  if (!validator(payload)) throw clientError("capacity.response_invalid", 502);
  return payload;
}

function validCapacityResponse(value) {
  return plain(value) && exact(value, [
    "services", "scenarios", "baselines", "forecasts", "load_tests", "chaos_faults",
    "chaos_experiments", "rollout_blocks", "request_id", "trace_id",
  ]) && bounded(value.services, 0, 100, validService)
    && bounded(value.scenarios, 0, 100, validScenario)
    && bounded(value.baselines, 0, 500, validBaseline)
    && bounded(value.forecasts, 0, 500, validForecast)
    && bounded(value.load_tests, 0, 500, validLoad)
    && bounded(value.chaos_faults, 0, 100, validFault)
    && bounded(value.chaos_experiments, 0, 500, validChaos)
    && bounded(value.rollout_blocks, 0, 500, validBlock)
    && links(value) && !privateKey(value);
}

function validService(value) {
  return shape(value, [
    "service_code", "region_code", "demand_rps", "capacity_rps", "utilization_bps",
    "headroom_rps", "threshold_bps", "threshold_reached", "slo_p95_ms",
    "recommendation_code", "revision", "updated_at",
  ]) && SERVICES.has(value.service_code) && REGION.test(value.region_code)
    && integer(value.demand_rps, 0) && integer(value.capacity_rps, 1)
    && integer(value.utilization_bps, 0) && Number.isSafeInteger(value.headroom_rps)
    && integer(value.threshold_bps, 5000, 9500) && typeof value.threshold_reached === "boolean"
    && integer(value.slo_p95_ms, 1, 60000) && recommendation(value.recommendation_code)
    && integer(value.revision, 1) && timestamp(value.updated_at);
}
function validScenario(value) {
  return shape(value, [
    "scenario_code", "service_code", "run_kind", "max_tenant_count", "max_target_rps",
    "max_duration_seconds", "target_peak_multiplier_bps", "manifest_sha256", "status",
  ]) && CODE.test(value.scenario_code) && SERVICES.has(value.service_code)
    && new Set(["load", "soak"]).has(value.run_kind) && integer(value.max_tenant_count, 1, 500)
    && integer(value.max_target_rps, 1, 100000) && integer(value.max_duration_seconds, 10, 259200)
    && integer(value.target_peak_multiplier_bps, 10000, 30000) && SHA256.test(value.manifest_sha256)
    && new Set(["active", "disabled"]).has(value.status);
}
function validBaseline(value) {
  return shape(value, [
    "performance_baseline_id", "service_code", "scenario_code", "environment_class",
    "version", "sample_count", "p50_ms", "p95_ms", "p99_ms", "throughput_rps_milli",
    "error_rate_bps", "created_at",
  ]) && OBJECT_ID.test(value.performance_baseline_id) && SERVICES.has(value.service_code)
    && CODE.test(value.scenario_code) && ENVIRONMENTS.has(value.environment_class)
    && integer(value.version, 1) && integer(value.sample_count, 100)
    && integer(value.p50_ms, 0) && integer(value.p95_ms, value.p50_ms)
    && integer(value.p99_ms, value.p95_ms) && integer(value.throughput_rps_milli, 0)
    && integer(value.error_rate_bps, 0, 10000) && timestamp(value.created_at);
}
function validForecast(value) {
  return shape(value, [
    "capacity_forecast_id", "service_code", "region_code", "horizon_days", "model_code",
    "predicted_demand_rps", "predicted_capacity_rps", "confidence_bps", "error_outcome_bps",
    "recommendation_code", "status", "job_id", "revision", "created_at", "updated_at",
  ]) && /^cfc_[0-9a-f]{32}$/u.test(value.capacity_forecast_id)
    && SERVICES.has(value.service_code) && REGION.test(value.region_code)
    && new Set([30, 90]).has(value.horizon_days) && value.model_code === "linear-v1"
    && optionalInteger(value.predicted_demand_rps, 0)
    && optionalInteger(value.predicted_capacity_rps, 1)
    && optionalInteger(value.confidence_bps, 0, 10000)
    && optionalInteger(value.error_outcome_bps, 0, 10000)
    && (value.recommendation_code === null || recommendation(value.recommendation_code))
    && new Set(["queued", "running", "succeeded", "failed"]).has(value.status)
    && JOB_ID.test(value.job_id) && integer(value.revision, 1)
    && timestamp(value.created_at) && timestamp(value.updated_at);
}
function validLoad(value) {
  return shape(value, [
    "load_test_run_id", "scenario_code", "service_code", "run_kind", "environment_class",
    "region_code", "synthetic_tenant_count", "target_rps", "duration_seconds", "status",
    "job_id", "sample_count", "p50_ms", "p95_ms", "p99_ms", "throughput_rps_milli",
    "error_rate_bps", "open_connection_delta", "handle_delta", "fairness_jain_millionths",
    "controlled_degradation", "regression_status", "safe_outcome_code", "revision",
    "started_at", "completed_at", "created_at", "updated_at",
  ]) && /^ltr_[0-9a-f]{32}$/u.test(value.load_test_run_id) && CODE.test(value.scenario_code)
    && SERVICES.has(value.service_code) && new Set(["load", "soak"]).has(value.run_kind)
    && ENVIRONMENTS.has(value.environment_class) && REGION.test(value.region_code)
    && integer(value.synthetic_tenant_count, 1, 500) && integer(value.target_rps, 1, 100000)
    && integer(value.duration_seconds, 10, 259200)
    && new Set(["queued", "running", "succeeded", "failed", "stopped"]).has(value.status)
    && JOB_ID.test(value.job_id) && optionalInteger(value.sample_count, 0)
    && optionalInteger(value.p50_ms, 0) && optionalInteger(value.p95_ms, 0)
    && optionalInteger(value.p99_ms, 0) && optionalInteger(value.throughput_rps_milli, 0)
    && optionalInteger(value.error_rate_bps, 0, 10000)
    && optionalInteger(value.open_connection_delta, 0) && optionalInteger(value.handle_delta, 0)
    && optionalInteger(value.fairness_jain_millionths, 0, 1000000)
    && optionalBoolean(value.controlled_degradation)
    && (value.regression_status === null || new Set([
      "no_baseline", "insufficient_confidence", "within_baseline", "regressed",
    ]).has(value.regression_status)) && optionalText(value.safe_outcome_code, 3, 96)
    && integer(value.revision, 1) && optionalTimestamp(value.started_at)
    && optionalTimestamp(value.completed_at) && timestamp(value.created_at) && timestamp(value.updated_at);
}
function validFault(value) {
  return shape(value, [
    "fault_code", "service_code", "max_blast_tenants", "max_duration_seconds",
    "manifest_sha256", "status",
  ]) && CODE.test(value.fault_code) && new Set([...SERVICES, "shared"]).has(value.service_code)
    && integer(value.max_blast_tenants, 1, 100) && integer(value.max_duration_seconds, 10, 3600)
    && SHA256.test(value.manifest_sha256) && new Set(["active", "disabled"]).has(value.status);
}
function validChaos(value) {
  return shape(value, [
    "chaos_experiment_id", "fault_code", "service_code", "environment_class", "region_code",
    "blast_tenant_count", "duration_seconds", "objective_p95_ms", "objective_error_rate_bps",
    "stop_error_rate_bps", "status", "job_id", "approvals", "kill_generation",
    "observed_p95_ms", "observed_error_rate_bps", "objective_met", "stop_reason_code",
    "revision", "started_at", "completed_at", "created_at", "updated_at",
  ]) && /^che_[0-9a-f]{32}$/u.test(value.chaos_experiment_id) && CODE.test(value.fault_code)
    && new Set([...SERVICES, "shared"]).has(value.service_code)
    && new Set(["staging", "production"]).has(value.environment_class)
    && REGION.test(value.region_code) && integer(value.blast_tenant_count, 1, 100)
    && integer(value.duration_seconds, 10, 3600) && integer(value.objective_p95_ms, 1, 60000)
    && integer(value.objective_error_rate_bps, 0, 10000)
    && integer(value.stop_error_rate_bps, value.objective_error_rate_bps, 10000)
    && new Set(["pending_approval", "queued", "running", "succeeded", "failed", "killed"])
      .has(value.status) && (value.job_id === null || JOB_ID.test(value.job_id))
    && bounded(value.approvals, 0, 2, validApproval) && integer(value.kill_generation, 0)
    && optionalInteger(value.observed_p95_ms, 0)
    && optionalInteger(value.observed_error_rate_bps, 0, 10000)
    && optionalBoolean(value.objective_met) && optionalText(value.stop_reason_code, 3, 96)
    && integer(value.revision, 1) && optionalTimestamp(value.started_at)
    && optionalTimestamp(value.completed_at) && timestamp(value.created_at) && timestamp(value.updated_at);
}
function validApproval(value) {
  return shape(value, ["approval_kind", "operator_id", "approved_at"])
    && new Set(["execution", "security_review"]).has(value.approval_kind)
    && /^pto_[0-9a-f]{32}$/u.test(value.operator_id) && timestamp(value.approved_at);
}
function validBlock(value) {
  return shape(value, [
    "service_code", "scenario_code", "environment_class", "load_test_run_id", "status",
    "created_at", "cleared_at",
  ]) && SERVICES.has(value.service_code) && CODE.test(value.scenario_code)
    && ENVIRONMENTS.has(value.environment_class) && /^ltr_[0-9a-f]{32}$/u.test(value.load_test_run_id)
    && new Set(["active", "cleared"]).has(value.status) && timestamp(value.created_at)
    && optionalTimestamp(value.cleared_at);
}
function validEconomicsResponse(value) {
  return shape(value, ["items", "total", "request_id", "trace_id"])
    && bounded(value.items, 0, 500, validEconomics) && integer(value.total, 0)
    && value.total >= value.items.length && links(value) && !privateKey(value);
}
function validEconomics(value) {
  return shape(value, [
    "unit_economics_snapshot_id", "billing_period", "service_code", "request_count",
    "input_tokens", "output_tokens", "total_tokens", "allocated_cost_micros",
    "cost_per_request_micros", "cost_per_million_tokens_micros", "currency",
    "usage_watermark", "allocation_version", "created_at",
  ]) && /^ues_[0-9a-f]{32}$/u.test(value.unit_economics_snapshot_id)
    && /^\d{4}-(?:0[1-9]|1[0-2])$/u.test(value.billing_period)
    && new Set([...SERVICES, "shared"]).has(value.service_code)
    && integer(value.request_count, 0) && integer(value.input_tokens, 0)
    && integer(value.output_tokens, 0) && integer(value.total_tokens, 0)
    && value.total_tokens === value.input_tokens + value.output_tokens
    && integer(value.allocated_cost_micros, 0)
    && optionalInteger(value.cost_per_request_micros, 0)
    && optionalInteger(value.cost_per_million_tokens_micros, 0)
    && new Set(["CNY", "USD"]).has(value.currency) && SHA256.test(value.usage_watermark)
    && integer(value.allocation_version, 1) && timestamp(value.created_at);
}
function validForecastResponse(value) {
  return shape(value, ["forecast", "idempotent_replay", "request_id", "trace_id"])
    && validForecast(value.forecast) && typeof value.idempotent_replay === "boolean" && links(value);
}
function validLoadResponse(value) {
  return shape(value, ["run", "idempotent_replay", "request_id", "trace_id"])
    && validLoad(value.run) && typeof value.idempotent_replay === "boolean" && links(value);
}
function validChaosResponse(value) {
  return shape(value, [
    "experiment", "approval_recorded", "idempotent_replay", "request_id", "trace_id",
  ]) && validChaos(value.experiment) && typeof value.approval_recorded === "boolean"
    && typeof value.idempotent_replay === "boolean" && links(value);
}
function recommendation(value) {
  return new Set(["hold", "scale-within-30d", "scale-within-90d"]).has(value);
}
function shape(value, keys) { return plain(value) && exact(value, keys); }
function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}
function optionalInteger(value, minimum, maximum) {
  return value === null || integer(value, minimum, maximum);
}
function optionalBoolean(value) { return value === null || typeof value === "boolean"; }
function optionalText(value, minimum, maximum) {
  return value === null || (typeof value === "string" && value.length >= minimum && value.length <= maximum);
}
function timestamp(value) { return typeof value === "string" && Number.isFinite(Date.parse(value)); }
function optionalTimestamp(value) { return value === null || timestamp(value); }
function bounded(value, minimum, maximum, validator) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum && value.every(validator);
}
function plain(value) { return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype; }
function exact(value, keys) {
  const actual = Object.keys(value).sort(); const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}
function links(value) { return LINK.test(value.request_id) && LINK.test(value.trace_id); }
function privateKey(value) {
  const blocked = new Set([
    "tenant_id", "workspace_id", "email", "contact", "token", "secret", "internal_endpoint",
  ]);
  if (Array.isArray(value)) return value.some(privateKey);
  if (!plain(value)) return false;
  return Object.entries(value).some(([key, item]) => blocked.has(key) || privateKey(item));
}
function apiUrl(controlApiUrl, path) {
  let base; try { base = new URL(String(controlApiUrl)); } catch { invalid(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) invalid();
  return new URL(path, base).toString();
}
function invalid() { throw clientError("capacity.request_invalid", 0); }
function clientError(code, status, requestId = "-") {
  return new PlatformApiError({
    code, status, requestId, message: "Capacity management request failed.",
  });
}
async function readJson(response) {
  try { const value = await response.json(); return plain(value) ? value : {}; } catch { return {}; }
}
