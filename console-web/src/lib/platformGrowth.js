/** Strict PRD-141 Platform Growth client. */

import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const ID = Object.freeze({
  campaign: /^gca_[0-9a-f]{32}$/u,
  experiment: /^gex_[0-9a-f]{32}$/u,
  export: /^gxp_[0-9a-f]{32}$/u,
  source: /^acs_[0-9a-f]{32}$/u,
});
const DATE = /^\d{4}-\d{2}-\d{2}$/u;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/u;
const CURSOR = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const OPAQUE = /^[A-Za-z0-9_-]{32,256}$/u;
const SAFE_CODE = /^[a-z0-9][a-z0-9._-]{0,79}$/u;
const STAGES = ["registered", "activated", "trial_started", "paid", "retained_30d"];
const DIMENSIONS = ["none", "source", "channel", "region", "plan_band"];
const FRESHNESS = ["fresh", "stale", "partial"];

export async function listGrowthFunnels({ controlApiUrl, filters = {}, cursor, limit = 100, signal } = {}) {
  return listRequest(controlApiUrl, "/platform/growth/funnels", filters, cursor, limit, 200,
    new Set(["view", "from", "to", "bucket", "dimension", "dimension_value", "formula_version"]),
    validateFunnelResponse, signal);
}

export async function listGrowthCohorts({ controlApiUrl, filters = {}, cursor, limit = 100, signal } = {}) {
  return listRequest(controlApiUrl, "/platform/growth/cohorts", filters, cursor, limit, 200,
    new Set(["view", "cohort_kind", "from", "to", "age_buckets", "dimension", "dimension_value", "formula_version"]),
    validateCohortResponse, signal);
}

export async function listGrowthCampaigns({ controlApiUrl, filters = {}, cursor, limit = 50, signal } = {}) {
  return listRequest(controlApiUrl, "/platform/growth/campaigns", filters, cursor, limit, 100,
    new Set(["status", "source", "channel", "from", "to"]), validateCampaignList, signal);
}

export async function applyGrowthCampaign({ controlApiUrl, command, idempotencyKey, stepUpGrant, signal } = {}) {
  const body = requestValue(() => campaignCommand(command));
  const value = await requestJson(controlApiUrl, "/platform/growth/campaigns", {
    method: "POST", body, revision: body.expected_revision,
    idempotencyKey: requestValue(() => pattern(idempotencyKey, IDEMPOTENCY, "idempotency_key")),
    stepUpGrant: requestValue(() => pattern(stepUpGrant, OPAQUE, "step_up_grant")), signal,
  });
  exact(value, ["campaign", "idempotent_replay", "request_id", "trace_id"]);
  boolean(value.idempotent_replay, "idempotent_replay");
  return Object.freeze({ campaign: campaign(value.campaign), idempotent_replay: value.idempotent_replay, ...trace(value) });
}

export async function listGrowthExperiments({ controlApiUrl, filters = {}, cursor, limit = 50, signal } = {}) {
  return listRequest(controlApiUrl, "/platform/growth/experiments", filters, cursor, limit, 100,
    new Set(["status", "surface", "metric", "from", "to"]), validateExperimentList, signal);
}

export async function applyGrowthExperiment({ controlApiUrl, command, idempotencyKey, stepUpGrant, signal } = {}) {
  const body = requestValue(() => experimentCommand(command));
  const value = await requestJson(controlApiUrl, "/platform/growth/experiments", {
    method: "POST", body, revision: body.expected_revision,
    idempotencyKey: requestValue(() => pattern(idempotencyKey, IDEMPOTENCY, "idempotency_key")),
    stepUpGrant: requestValue(() => pattern(stepUpGrant, OPAQUE, "step_up_grant")), signal,
  });
  exact(value, ["experiment", "idempotent_replay", "request_id", "trace_id"]);
  boolean(value.idempotent_replay, "idempotent_replay");
  return Object.freeze({ experiment: experiment(value.experiment), idempotent_replay: value.idempotent_replay, ...trace(value) });
}

export async function listGrowthAttribution({ controlApiUrl, filters = {}, cursor, limit = 100, signal } = {}) {
  return listRequest(controlApiUrl, "/platform/growth/attribution", filters, cursor, limit, 200,
    new Set(["view", "from", "to", "stage", "formula_version", "source", "channel", "campaign"]),
    validateAttributionResponse, signal);
}

export async function requestGrowthExport({ controlApiUrl, manifest, idempotencyKey, stepUpGrant, signal } = {}) {
  const body = requestValue(() => exportManifest(manifest));
  return exportResponse(await requestJson(controlApiUrl, "/platform/growth/exports", {
    method: "POST", body, revision: 0,
    idempotencyKey: requestValue(() => pattern(idempotencyKey, IDEMPOTENCY, "idempotency_key")),
    stepUpGrant: requestValue(() => pattern(stepUpGrant, OPAQUE, "step_up_grant")), signal,
  }));
}

export async function getGrowthExport({ controlApiUrl, exportId, signal } = {}) {
  const id = requestValue(() => pattern(exportId, ID.export, "export_id"));
  return exportResponse(await requestJson(controlApiUrl, `/platform/growth/exports/${encodeURIComponent(id)}`, { signal }));
}

export async function decideGrowthExport({ controlApiUrl, exportId, command, idempotencyKey, stepUpGrant, signal } = {}) {
  const id = requestValue(() => pattern(exportId, ID.export, "export_id"));
  const body = requestValue(() => approvalCommand(command));
  return exportResponse(await requestJson(controlApiUrl, `/platform/growth/exports/${encodeURIComponent(id)}/approvals`, {
    method: "POST", body, revision: body.expected_revision,
    idempotencyKey: requestValue(() => pattern(idempotencyKey, IDEMPOTENCY, "idempotency_key")),
    stepUpGrant: requestValue(() => pattern(stepUpGrant, OPAQUE, "step_up_grant")), signal,
  }));
}

export async function issueGrowthExportReceipt({ controlApiUrl, exportId, stepUpGrant, signal } = {}) {
  const id = requestValue(() => pattern(exportId, ID.export, "export_id"));
  const value = await requestJson(controlApiUrl, `/platform/growth/exports/${encodeURIComponent(id)}/receipts`, {
    method: "POST", body: { confirmed: true },
    stepUpGrant: requestValue(() => pattern(stepUpGrant, OPAQUE, "step_up_grant")), signal,
  });
  exact(value, ["export_id", "receipt", "expires_at", "request_id", "trace_id"]);
  if (value.export_id !== id) invalidResponse("receipt.owner");
  pattern(value.receipt, OPAQUE, "receipt");
  timestamp(value.expires_at, "expires_at");
  return Object.freeze({ ...value, ...trace(value) });
}

export async function downloadGrowthExport({ controlApiUrl, exportId, receipt, signal } = {}) {
  const id = requestValue(() => pattern(exportId, ID.export, "export_id"));
  const token = requestValue(() => pattern(receipt, OPAQUE, "receipt"));
  const response = await rawRequest(controlApiUrl, `/platform/growth/exports/${encodeURIComponent(id)}/download`, {
    headers: { "X-Growth-Export-Receipt": token }, signal,
  });
  if (!response.ok) await throwResponseError(response);
  const contentType = response.headers.get("content-type") ?? "";
  const disposition = response.headers.get("content-disposition") ?? "";
  if (!contentType.toLowerCase().startsWith("text/csv")
    || !/^attachment;/iu.test(disposition)
    || response.headers.get("x-content-type-options") !== "nosniff") {
    invalidResponse("download.headers");
  }
  const filename = disposition.match(/filename="([A-Za-z0-9._-]{1,128})"/u)?.[1]
    ?? `growth-${id}.csv`;
  return Object.freeze({ blob: await response.blob(), filename });
}

async function listRequest(controlApiUrl, path, filters, cursor, limit, maximum, allowed, validate, signal) {
  plain(filters, "filters", invalidRequest);
  const parameters = new URLSearchParams();
  for (const key of Object.keys(filters).sort()) {
    if (!allowed.has(key)) invalidRequest(`filter.${key}`);
    const value = requestString(filters[key], `filter.${key}`);
    if (["from", "to"].includes(key)) date(value, key, invalidRequest);
    validateListFilter(path, key, value);
    parameters.set(key, value);
  }
  if (filters.from && filters.to && filters.from > filters.to) invalidRequest("filter.range");
  parameters.set("limit", String(requestInteger(limit, 1, maximum, "limit")));
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    parameters.set("cursor", pattern(cursor, CURSOR, "cursor", 2048, invalidRequest));
  }
  return validate(await requestJson(controlApiUrl, `${path}?${parameters.toString()}`, { signal }));
}

function validateFunnelResponse(value) {
  const response = listEnvelope(value, ["formula_version", "schema_version", "source_freshness", "generated_at"], 200);
  if (value.schema_version !== "platform-growth-funnel-v1") invalidResponse("schema_version");
  safeString(value.formula_version, 1, 80, "formula_version");
  timestamp(value.generated_at, "generated_at");
  return Object.freeze({ ...response, items: frozen(value.items.map(funnelBucket)),
    formula_version: value.formula_version, schema_version: value.schema_version,
    source_freshness: freshness(value.source_freshness), generated_at: value.generated_at });
}

function validateCohortResponse(value) {
  const response = listEnvelope(value, ["formula_version", "schema_version", "source_freshness", "generated_at"], 200);
  if (value.schema_version !== "platform-growth-cohort-v1") invalidResponse("schema_version");
  safeString(value.formula_version, 1, 80, "formula_version");
  timestamp(value.generated_at, "generated_at");
  return Object.freeze({ ...response, items: frozen(value.items.map(cohortBucket)),
    formula_version: value.formula_version, schema_version: value.schema_version,
    source_freshness: freshness(value.source_freshness), generated_at: value.generated_at });
}

function validateCampaignList(value) {
  const response = listEnvelope(value, ["schema_version", "source_freshness", "generated_at"], 100);
  if (value.schema_version !== "platform-growth-campaign-v1") invalidResponse("schema_version");
  timestamp(value.generated_at, "generated_at");
  return Object.freeze({ ...response, items: frozen(value.items.map(campaign)),
    schema_version: value.schema_version, source_freshness: freshness(value.source_freshness),
    generated_at: value.generated_at });
}

function validateExperimentList(value) {
  const response = listEnvelope(value, ["schema_version", "source_freshness", "generated_at"], 100);
  if (value.schema_version !== "platform-growth-experiment-v1") invalidResponse("schema_version");
  timestamp(value.generated_at, "generated_at");
  return Object.freeze({ ...response, items: frozen(value.items.map(experiment)),
    schema_version: value.schema_version, source_freshness: freshness(value.source_freshness),
    generated_at: value.generated_at });
}

function validateAttributionResponse(value) {
  const response = listEnvelope(value, ["view", "formula_version", "schema_version", "source_freshness", "generated_at"], 200);
  choice(value.view, ["summary", "campaign", "channel", "channel_summary"], "view");
  if (value.formula_version !== "last-eligible-touch-30d-v1"
    || value.schema_version !== "platform-growth-attribution-v1") invalidResponse("schema");
  timestamp(value.generated_at, "generated_at");
  return Object.freeze({ ...response, view: value.view, items: frozen(value.items.map(attribution)),
    formula_version: value.formula_version, schema_version: value.schema_version,
    source_freshness: freshness(value.source_freshness), generated_at: value.generated_at });
}

function listEnvelope(value, extra, maximum) {
  exact(value, ["items", "limit", "has_more", "next_cursor", "request_id", "trace_id", ...extra]);
  array(value.items, 0, maximum, "items");
  integer(value.limit, 1, maximum, "limit");
  boolean(value.has_more, "has_more");
  if (value.next_cursor !== null) pattern(value.next_cursor, CURSOR, "next_cursor", 2048);
  if (value.has_more !== (value.next_cursor !== null) || value.items.length > value.limit) invalidResponse("pagination");
  return { limit: value.limit, has_more: value.has_more, next_cursor: value.next_cursor, ...trace(value) };
}

function funnelBucket(value) {
  exact(value, ["snapshot_id", "bucket_start", "bucket", "view", "dimension", "dimension_value", "stages", "coverage_bps", "suppressed", "suppression_code", "currency", "revenue_minor", "cost_minor", "contribution_minor", "generation", "source_watermark"]);
  pattern(value.snapshot_id, /^gfs_[0-9a-f]{32}$/u, "snapshot_id");
  date(value.bucket_start, "bucket_start"); choice(value.bucket, ["day", "week", "month"], "bucket");
  choice(value.view, ["overview", "adoption", "retention", "legacy_operational_v1"], "view");
  choice(value.dimension, DIMENSIONS, "dimension"); nullableString(value.dimension_value, 1, 80, "dimension_value");
  array(value.stages, 5, 5, "stages"); value.stages.forEach(stage);
  privacy(value); integer(value.generation, 1, Number.MAX_SAFE_INTEGER, "generation");
  nullableTimestamp(value.source_watermark, "source_watermark");
  for (const key of ["revenue_minor", "cost_minor", "contribution_minor"]) nullableInteger(value[key], -Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER, key);
  nullableString(value.currency, 3, 3, "currency");
  return Object.freeze({ ...value, stages: frozen(value.stages.map((item) => Object.freeze({ ...item }))) });
}

function stage(value) {
  exact(value, ["code", "subject_count", "event_count", "conversion_from_previous_bps"]);
  choice(value.code, STAGES, "stage.code");
  nullableInteger(value.subject_count, 0, Number.MAX_SAFE_INTEGER, "subject_count");
  nullableInteger(value.event_count, 0, Number.MAX_SAFE_INTEGER, "event_count");
  nullableInteger(value.conversion_from_previous_bps, 0, 10000, "conversion_bps");
}

function cohortBucket(value) {
  exact(value, ["snapshot_id", "cohort_kind", "cohort_bucket", "age_bucket", "view", "dimension", "dimension_value", "eligible_count", "retained_count", "churned_count", "retention_bps", "coverage_bps", "suppressed", "suppression_code", "generation", "supersedes_snapshot_id", "source_watermark"]);
  pattern(value.snapshot_id, /^gco_[0-9a-f]{32}$/u, "snapshot_id");
  choice(value.cohort_kind, ["signup_week", "activation_week", "first_paid_month", "campaign"], "cohort_kind");
  date(value.cohort_bucket, "cohort_bucket"); integer(value.age_bucket, 0, 23, "age_bucket");
  choice(value.view, ["adoption", "retention"], "view"); choice(value.dimension, DIMENSIONS, "dimension");
  nullableString(value.dimension_value, 1, 80, "dimension_value"); privacy(value);
  for (const key of ["eligible_count", "retained_count", "churned_count"]) nullableInteger(value[key], 0, Number.MAX_SAFE_INTEGER, key);
  nullableInteger(value.retention_bps, 0, 10000, "retention_bps");
  integer(value.generation, 1, Number.MAX_SAFE_INTEGER, "generation");
  nullablePattern(value.supersedes_snapshot_id, /^gco_[0-9a-f]{32}$/u, "supersedes_snapshot_id");
  nullableTimestamp(value.source_watermark, "source_watermark");
  return Object.freeze({ ...value });
}

function campaign(value) {
  exact(value, ["campaign_id", "display_name", "acquisition_source_id", "channel_code", "target_stage", "starts_at", "ends_at", "budget_minor", "currency", "status", "revision", "conversion_count", "spend_minor", "outcome_bps", "action_capabilities", "updated_at"]);
  pattern(value.campaign_id, ID.campaign, "campaign_id"); pattern(value.acquisition_source_id, ID.source, "source_id");
  safeString(value.display_name, 1, 120, "display_name"); pattern(value.channel_code, SAFE_CODE, "channel_code");
  choice(value.target_stage, STAGES, "target_stage"); timestamp(value.starts_at, "starts_at"); timestamp(value.ends_at, "ends_at");
  choice(value.status, ["draft", "scheduled", "active", "paused", "completed", "archived"], "status");
  for (const key of ["budget_minor", "conversion_count", "spend_minor"]) integer(value[key], 0, Number.MAX_SAFE_INTEGER, key);
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision"); nullableInteger(value.outcome_bps, 0, 10000, "outcome_bps");
  safeString(value.currency, 3, 3, "currency"); stringArray(value.action_capabilities, "action_capabilities"); timestamp(value.updated_at, "updated_at");
  return Object.freeze({ ...value, action_capabilities: frozen([...value.action_capabilities]) });
}

function experiment(value) {
  exact(value, ["experiment_id", "hypothesis_code", "surface_code", "primary_metric", "control_code", "variant_code", "control_allocation_bps", "variant_allocation_bps", "eligibility_policy_version", "consent_version", "sample_cap", "starts_at", "ends_at", "guardrail_code", "decision_policy_version", "status", "decision", "assigned_count", "control_count", "variant_count", "revision", "action_capabilities", "updated_at"]);
  pattern(value.experiment_id, ID.experiment, "experiment_id");
  for (const key of ["hypothesis_code", "surface_code", "primary_metric", "eligibility_policy_version", "consent_version", "guardrail_code", "decision_policy_version"]) safeString(value[key], 1, 80, key);
  safeString(value.control_code, 1, 40, "control_code"); safeString(value.variant_code, 1, 40, "variant_code");
  integer(value.control_allocation_bps, 1, 9999, "control_allocation_bps"); integer(value.variant_allocation_bps, 1, 9999, "variant_allocation_bps");
  if (value.control_allocation_bps + value.variant_allocation_bps !== 10000) invalidResponse("allocation");
  integer(value.sample_cap, 20, 1e7, "sample_cap"); timestamp(value.starts_at, "starts_at"); timestamp(value.ends_at, "ends_at");
  choice(value.status, ["draft", "scheduled", "running", "paused", "decided", "stopped", "canceled", "archived"], "status");
  nullableString(value.decision, 1, 40, "decision");
  for (const key of ["assigned_count", "control_count", "variant_count"]) integer(value[key], 0, Number.MAX_SAFE_INTEGER, key);
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision"); stringArray(value.action_capabilities, "action_capabilities"); timestamp(value.updated_at, "updated_at");
  return Object.freeze({ ...value, action_capabilities: frozen([...value.action_capabilities]) });
}

function attribution(value) {
  exact(value, ["safe_id", "source_id", "campaign_id", "channel_partner_id", "conversion_stage", "conversion_count", "value_minor", "cost_minor", "currency", "conversion_bps", "coverage_bps", "suppressed", "suppression_code", "source_watermark"]);
  safeString(value.safe_id, 1, 160, "safe_id"); nullablePattern(value.source_id, ID.source, "source_id");
  nullablePattern(value.campaign_id, ID.campaign, "campaign_id"); nullablePattern(value.channel_partner_id, /^chp_[0-9a-f]{32}$/u, "channel_partner_id");
  choice(value.conversion_stage, STAGES, "conversion_stage"); privacy(value);
  for (const key of ["conversion_count", "value_minor", "cost_minor"]) nullableInteger(value[key], 0, Number.MAX_SAFE_INTEGER, key);
  nullableInteger(value.conversion_bps, 0, 10000, "conversion_bps"); nullableString(value.currency, 3, 3, "currency");
  nullableTimestamp(value.source_watermark, "source_watermark"); return Object.freeze({ ...value });
}

function freshness(value) {
  exact(value, ["status", "watermark", "stale_seconds", "coverage_bps"]);
  choice(value.status, FRESHNESS, "freshness.status"); nullableTimestamp(value.watermark, "watermark");
  integer(value.stale_seconds, 0, Number.MAX_SAFE_INTEGER, "stale_seconds"); integer(value.coverage_bps, 0, 10000, "coverage_bps");
  return Object.freeze({ ...value });
}

function privacy(value) {
  integer(value.coverage_bps, 0, 10000, "coverage_bps"); boolean(value.suppressed, "suppressed");
  if (value.suppression_code !== null) choice(value.suppression_code, ["k_anonymity", "complementary", "privacy_budget"], "suppression_code");
  if (value.suppressed !== (value.suppression_code !== null)) invalidResponse("suppression");
}

function exportResponse(value) {
  exact(value, ["export", "idempotent_replay", "request_id", "trace_id"]);
  boolean(value.idempotent_replay, "idempotent_replay");
  return Object.freeze({ export: exportSummary(value.export), idempotent_replay: value.idempotent_replay, ...trace(value) });
}

function exportSummary(value) {
  exact(value, ["export_id", "query_kind", "sensitivity", "status", "revision", "row_count", "cell_count", "expires_at", "download_count", "created_at", "updated_at"]);
  pattern(value.export_id, ID.export, "export_id"); choice(value.query_kind, ["funnels", "cohorts", "campaigns", "experiments", "attribution"], "query_kind");
  choice(value.sensitivity, ["aggregate", "high_cardinality"], "sensitivity");
  choice(value.status, ["requested", "policy_review", "approved", "rejected", "building", "ready", "downloaded", "expired", "revoked", "failed"], "status");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision"); nullableInteger(value.row_count, 0, Number.MAX_SAFE_INTEGER, "row_count"); nullableInteger(value.cell_count, 0, Number.MAX_SAFE_INTEGER, "cell_count");
  nullableTimestamp(value.expires_at, "expires_at"); integer(value.download_count, 0, 1, "download_count"); timestamp(value.created_at, "created_at"); timestamp(value.updated_at, "updated_at");
  return Object.freeze({ ...value });
}

function campaignCommand(value) {
  plain(value, "command", invalidRequest); choice(value.operation, ["create", "update", "schedule", "pause", "resume", "complete", "archive"], "operation", invalidRequest);
  const config = ["display_name", "acquisition_source_id", "channel_code", "target_stage", "starts_at", "ends_at", "budget_minor", "currency"];
  const keys = value.operation === "create" ? ["operation", "expected_revision", ...config, "confirmed"]
    : value.operation === "update" ? ["operation", "campaign_id", "expected_revision", ...config, "confirmed"]
      : ["operation", "campaign_id", "expected_revision", "confirmed"];
  exact(value, keys, invalidRequest); requestInteger(value.expected_revision, value.operation === "create" ? 0 : 1, Number.MAX_SAFE_INTEGER, "expected_revision");
  if (value.operation !== "create") pattern(value.campaign_id, ID.campaign, "campaign_id", 160, invalidRequest);
  if (config.every((key) => Object.hasOwn(value, key))) {
    requestString(value.display_name, "display_name"); pattern(value.acquisition_source_id, ID.source, "source_id", 160, invalidRequest);
    pattern(value.channel_code, SAFE_CODE, "channel_code", 80, invalidRequest); if (!STAGES.includes(value.target_stage)) invalidRequest("target_stage");
    timestamp(value.starts_at, "starts_at", invalidRequest); timestamp(value.ends_at, "ends_at", invalidRequest); requestInteger(value.budget_minor, 0, Number.MAX_SAFE_INTEGER, "budget_minor");
    if (value.starts_at >= value.ends_at) invalidRequest("campaign.range");
    if (!/^[A-Z]{3}$/u.test(value.currency)) invalidRequest("currency");
  }
  if (value.confirmed !== true) invalidRequest("confirmed"); return { ...value };
}

function experimentCommand(value) {
  plain(value, "command", invalidRequest); choice(value.operation, ["create", "update", "schedule", "start", "pause", "resume", "stop", "decide", "archive"], "operation", invalidRequest);
  const config = ["hypothesis_code", "surface_code", "primary_metric", "control_code", "variant_code", "control_allocation_bps", "variant_allocation_bps", "eligibility_policy_version", "consent_version", "sample_cap", "starts_at", "ends_at", "guardrail_code", "decision_policy_version"];
  const source = ["start", "decide"].includes(value.operation) ? ["source_watermark"] : [];
  const keys = value.operation === "create" ? ["operation", "expected_revision", ...config, "confirmed"]
    : value.operation === "update" ? ["operation", "experiment_id", "expected_revision", ...config, "confirmed"]
      : ["operation", "experiment_id", "expected_revision", ...source, "confirmed"];
  exact(value, keys, invalidRequest); requestInteger(value.expected_revision, value.operation === "create" ? 0 : 1, Number.MAX_SAFE_INTEGER, "expected_revision");
  if (value.operation !== "create") pattern(value.experiment_id, ID.experiment, "experiment_id", 160, invalidRequest);
  for (const key of config) if (Object.hasOwn(value, key) && typeof value[key] === "string") requestString(value[key], key);
  if (Object.hasOwn(value, "control_allocation_bps")) {
    requestInteger(value.control_allocation_bps, 1, 9999, "control_allocation_bps"); requestInteger(value.variant_allocation_bps, 1, 9999, "variant_allocation_bps");
    requestInteger(value.sample_cap, 20, 1e7, "sample_cap"); timestamp(value.starts_at, "starts_at", invalidRequest); timestamp(value.ends_at, "ends_at", invalidRequest);
    if (value.control_allocation_bps + value.variant_allocation_bps !== 10000) invalidRequest("allocation");
    if (value.starts_at >= value.ends_at) invalidRequest("experiment.range");
  }
  if (source.length) requestString(value.source_watermark, "source_watermark");
  if (value.confirmed !== true) invalidRequest("confirmed"); return { ...value };
}

function exportManifest(value) {
  exact(value, ["query_kind", "view", "from_date", "to_date", "bucket", "dimension", "formula_version", "sensitivity", "confirmed"], invalidRequest);
  choice(value.query_kind, ["funnels", "cohorts", "campaigns", "experiments", "attribution"], "query_kind", invalidRequest);
  requestString(value.view, "view"); date(value.from_date, "from_date", invalidRequest); date(value.to_date, "to_date", invalidRequest);
  if (value.query_kind === "funnels") choice(value.bucket, ["day", "week", "month"], "bucket", invalidRequest); else if (value.bucket !== null) invalidRequest("bucket");
  choice(value.dimension, DIMENSIONS, "dimension", invalidRequest); requestString(value.formula_version, "formula_version"); choice(value.sensitivity, ["aggregate", "high_cardinality"], "sensitivity", invalidRequest);
  if (value.confirmed !== true || value.from_date > value.to_date) invalidRequest("manifest"); return { ...value };
}

function approvalCommand(value) {
  exact(value, ["operation", "expected_revision", "reason_code", "confirmed"], invalidRequest);
  choice(value.operation, ["approve", "reject"], "operation", invalidRequest); requestInteger(value.expected_revision, 1, Number.MAX_SAFE_INTEGER, "expected_revision");
  const reason = value.operation === "approve" ? "policy_approved" : "policy_rejected";
  if (value.reason_code !== reason || value.confirmed !== true) invalidRequest("approval"); return { ...value };
}

function validateListFilter(path, key, value) {
  const choices = {
    "/platform/growth/funnels": {
      view: ["overview", "adoption", "retention"],
      bucket: ["day", "week", "month"],
      dimension: DIMENSIONS,
      formula_version: ["growth-funnel-v2"],
    },
    "/platform/growth/cohorts": {
      view: ["adoption", "retention"],
      cohort_kind: ["signup_week", "activation_week", "first_paid_month", "campaign"],
      dimension: DIMENSIONS,
      formula_version: ["growth-cohort-v2"],
    },
    "/platform/growth/campaigns": {
      status: ["draft", "scheduled", "active", "paused", "completed", "archived"],
    },
    "/platform/growth/experiments": {
      status: ["draft", "scheduled", "running", "paused", "decided", "stopped", "canceled", "archived"],
    },
    "/platform/growth/attribution": {
      view: ["summary", "campaign", "channel", "channel_summary"],
      stage: STAGES,
      formula_version: ["last-eligible-touch-30d-v1"],
    },
  }[path] ?? {};
  if (choices[key] && !choices[key].includes(value)) invalidRequest(`filter.${key}`);
  if (key === "age_buckets") {
    const count = Number(value);
    if (!Number.isSafeInteger(count) || String(count) !== value || count < 1 || count > 24) {
      invalidRequest("filter.age_buckets");
    }
  }
  if (key === "source") pattern(value, ID.source, "filter.source", 160, invalidRequest);
  if (key === "campaign") pattern(value, ID.campaign, "filter.campaign", 160, invalidRequest);
  if (["channel", "surface", "metric"].includes(key)) {
    pattern(value, SAFE_CODE, `filter.${key}`, 80, invalidRequest);
  }
}

async function requestJson(controlApiUrl, path, options = {}) {
  const response = await rawRequest(controlApiUrl, path, options);
  const payload = await responseJson(response);
  if (!response.ok) throwApiError(response, payload);
  return payload;
}

async function rawRequest(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers ?? {}) };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = readPlatformCsrfCookie();
  }
  if (options.revision !== undefined) headers["If-Match"] = `"${options.revision}"`;
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  if (options.stepUpGrant) headers["X-Step-Up-Grant"] = options.stepUpGrant;
  try {
    return await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET", headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include", cache: "no-store", redirect: "error", signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new PlatformApiError({ code: "platform.network_unavailable", message: "Platform Growth is temporarily unreachable." });
  }
}

async function throwResponseError(response) {
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  throwApiError(response, payload);
}

function throwApiError(response, payload) {
  throw new PlatformApiError({ status: response.status,
    code: safeStringOr(payload?.code, "platform.request_failed"),
    message: safeStringOr(payload?.message, "Platform Growth request failed."),
    requestId: safeStringOr(payload?.request_id, "-") });
}

function apiUrl(baseUrl, path) {
  let base;
  try { base = new URL(String(baseUrl)); } catch { invalidRequest("control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) invalidRequest("control_api_url");
  return new URL(path, base).toString();
}

async function responseJson(response) {
  try { const value = await response.json(); return plain(value, "response"); }
  catch { invalidResponse("json"); }
}

function trace(value) { safeString(value.request_id, 1, 160, "request_id"); safeString(value.trace_id, 1, 160, "trace_id"); return { request_id: value.request_id, trace_id: value.trace_id }; }
function exact(value, keys, fail = invalidResponse) { plain(value, "object", fail); const actual = Object.keys(value).sort(); const expected = [...keys].sort(); if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) fail("shape"); return value; }
function plain(value, label, fail = invalidResponse) { if (!value || Object.getPrototypeOf(value) !== Object.prototype) fail(label); return value; }
function array(value, min, max, label) { if (!Array.isArray(value) || value.length < min || value.length > max) invalidResponse(label); return value; }
function stringArray(value, label) { array(value, 0, 20, label); value.forEach((item) => safeString(item, 1, 40, label)); }
function frozen(value) { return Object.freeze(value); }
function safeString(value, min, max, label, fail = invalidResponse) { if (typeof value !== "string" || value.length < min || value.length > max || /[\u0000-\u001f\u007f]/u.test(value)) fail(label); return value; }
function requestString(value, label) { return safeString(value, 1, 160, label, invalidRequest); }
function safeStringOr(value, fallback) { return typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240) : fallback; }
function integer(value, min, max, label) { if (!Number.isSafeInteger(value) || value < min || value > max) invalidResponse(label); return value; }
function requestInteger(value, min, max, label) { if (!Number.isSafeInteger(value) || value < min || value > max) invalidRequest(label); return value; }
function nullableInteger(value, min, max, label) { return value === null ? null : integer(value, min, max, label); }
function boolean(value, label) { if (typeof value !== "boolean") invalidResponse(label); return value; }
function choice(value, values, label, fail = invalidResponse) { if (!values.includes(value)) fail(label); return value; }
function pattern(value, regex, label, max = 160, fail = invalidResponse) { if (typeof value !== "string" || value.length > max || !regex.test(value)) fail(label); return value; }
function nullablePattern(value, regex, label) { return value === null ? null : pattern(value, regex, label); }
function nullableString(value, min, max, label) { return value === null ? null : safeString(value, min, max, label); }
function timestamp(value, label, fail = invalidResponse) { if (typeof value !== "string" || !TIMESTAMP.test(value) || !Number.isFinite(Date.parse(value))) fail(label); return value; }
function nullableTimestamp(value, label) { return value === null ? null : timestamp(value, label); }
function date(value, label, fail = invalidResponse) { if (typeof value !== "string" || !DATE.test(value) || !Number.isFinite(Date.parse(`${value}T00:00:00Z`))) fail(label); return value; }
function requestValue(build) { try { return build(); } catch (error) { if (error instanceof PlatformApiError && error.code === "platform.growth_response_invalid") invalidRequest("request"); throw error; } }
function invalidRequest(label = "request") { throw new PlatformApiError({ code: "platform.request_invalid", message: `Invalid Platform Growth request: ${String(label).slice(0, 80)}` }); }
function invalidResponse(label = "response") { throw new PlatformApiError({ status: 502, code: "platform.growth_response_invalid", message: `Invalid Platform Growth response: ${String(label).slice(0, 80)}` }); }
