/** Strict PRD-139 Platform channel-management client. */

import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const ID = Object.freeze({
  account: /^gac_[0-9a-f]{32}$/u,
  approval: /^aca_[0-9a-f]{32}$/u,
  attribution: /^wat_[0-9a-f]{32}$/u,
  change: /^atc_[0-9a-f]{32}$/u,
  lead: /^cld_[0-9a-f]{32}$/u,
  membership: /^chm_[0-9a-f]{32}$/u,
  operator: /^pto_[0-9a-f]{32}$/u,
  partner: /^par_[0-9a-f]{32}$/u,
  referral: /^crc_[0-9a-f]{32}$/u,
  rule: /^ccr_[0-9a-f]{32}$/u,
  statement: /^cst_[0-9a-f]{32}$/u,
  workspace: /^wsp_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{2,79}$/u;
const PARTNER_CODE = /^[a-z0-9][a-z0-9._-]{0,79}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const OPAQUE = /^[A-Za-z0-9_-]{43,128}$/u;
const TIERS = ["registered", "silver", "gold", "strategic"];
const REGIONS = ["global", "cn", "apac", "emea", "americas"];
const PARTNER_STATUSES = ["active", "suspended", "retired"];
const PARTNER_ROLES = ["partner_owner", "partner_operations", "partner_finance"];

export async function listChannelPartners({
  controlApiUrl, status = "", tierCode = "", regionScope = "", limit = 50, offset = 0, signal,
} = {}) {
  const query = pagination(limit, offset);
  optionalChoice(query, "status", status, PARTNER_STATUSES);
  optionalChoice(query, "tier_code", tierCode, TIERS);
  optionalChoice(query, "region_scope", regionScope, REGIONS);
  const value = await requestJson(
    controlApiUrl, `/platform/channel-partners?${query.toString()}`, { signal },
  );
  exact(value, ["items", "limit", "offset", "request_id", "total", "trace_id"]);
  array(value.items, 100, "items");
  return freeze({
    items: value.items.map(partnerSummary),
    total: integer(value.total, 0, "total"),
    limit: integer(value.limit, 1, "limit", 100),
    offset: integer(value.offset, 0, "offset", 1_000_000),
    ...trace(value),
  });
}

export async function commandChannelPartner({
  controlApiUrl, command, expectedRevision, stepUpGrant, idempotencyKey, signal,
} = {}) {
  const body = partnerCommand(command);
  const revision = requestInteger(expectedRevision, 0, "expected_revision");
  if ((body.expected_revision ?? 0) !== revision) invalidRequest("expected_revision");
  const value = await post(controlApiUrl, "/platform/channel-partners", {
    body, revision, stepUpGrant, idempotencyKey, signal,
  });
  exact(value, [
    "idempotent_replay", "issued_referral_code", "partner", "request_id", "trace_id",
  ]);
  const partner = partnerSummary(value.partner);
  return freeze({
    partner,
    issued_referral_code: value.issued_referral_code === null
      ? null : pattern(value.issued_referral_code, /^[A-Za-z0-9_-]{20,128}$/u, "referral_code"),
    idempotent_replay: boolean(value.idempotent_replay, "idempotent_replay"),
    ...trace(value),
  });
}

export async function registerChannelLead({
  controlApiUrl, command, stepUpGrant, idempotencyKey, signal,
} = {}) {
  const body = leadCommand(command);
  const value = await post(controlApiUrl, "/platform/channel-leads", {
    body, revision: body.expected_revision, stepUpGrant, idempotencyKey, signal,
  });
  exact(value, ["idempotent_replay", "lead", "request_id", "trace_id"]);
  return freeze({
    lead: leadSummary(value.lead),
    idempotent_replay: boolean(value.idempotent_replay, "idempotent_replay"),
    ...trace(value),
  });
}

export async function requestChannelAttribution({
  controlApiUrl, command, stepUpGrant, idempotencyKey, signal,
} = {}) {
  const body = attributionRequest(command);
  const value = await post(controlApiUrl, "/platform/workspace-attributions", {
    body, revision: body.expected_attribution_revision, stepUpGrant, idempotencyKey, signal,
  });
  return attributionResponse(value);
}

export async function approveChannelAttribution({
  controlApiUrl, attributionId, command, stepUpGrant, idempotencyKey, signal,
} = {}) {
  const checkedId = requestPattern(attributionId, ID.attribution, "workspace_attribution_id");
  const body = attributionApproval(command);
  const value = await post(
    controlApiUrl,
    `/platform/workspace-attributions/${encodeURIComponent(checkedId)}/approve`,
    { body, revision: body.expected_revision, stepUpGrant, idempotencyKey, signal },
  );
  const result = attributionResponse(value);
  if (result.attribution.workspace_attribution_id !== checkedId) invalidResponse("attribution_owner");
  return result;
}

export async function listChannelStatements({
  controlApiUrl, channelPartnerId = "", currency = "", limit = 50, offset = 0, signal,
} = {}) {
  const query = pagination(limit, offset);
  if (channelPartnerId) query.set(
    "channel_partner_id", requestPattern(channelPartnerId, ID.partner, "channel_partner_id"),
  );
  optionalChoice(query, "currency", currency, ["CNY", "USD"]);
  const value = await requestJson(
    controlApiUrl, `/platform/channel-statements?${query.toString()}`, { signal },
  );
  exact(value, ["items", "limit", "offset", "request_id", "total", "trace_id"]);
  array(value.items, 100, "items");
  return freeze({
    items: value.items.map(statementSummary),
    total: integer(value.total, 0, "total"),
    limit: integer(value.limit, 1, "limit", 100),
    offset: integer(value.offset, 0, "offset", 1_000_000),
    ...trace(value),
  });
}

function partnerCommand(value) {
  plain(value, "command", invalidRequest);
  const operation = requestChoice(value.operation, [
    "create", "update", "upsert_membership", "revoke_membership", "rotate_referral_code",
  ], "operation");
  if (operation === "create") {
    exact(value, [
      "confirmed", "contract_sha256", "display_name", "operation", "owner_account_id",
      "owner_workspace_id", "partner_code", "region_scope", "tier_code",
    ], invalidRequest);
    return {
      operation,
      owner_workspace_id: requestPattern(value.owner_workspace_id, ID.workspace, "owner_workspace_id"),
      owner_account_id: requestPattern(value.owner_account_id, ID.account, "owner_account_id"),
      partner_code: requestPattern(value.partner_code, PARTNER_CODE, "partner_code"),
      display_name: boundedText(value.display_name, 1, 120, "display_name", invalidRequest),
      tier_code: requestChoice(value.tier_code, TIERS, "tier_code"),
      region_scope: requestChoice(value.region_scope, REGIONS, "region_scope"),
      contract_sha256: requestPattern(value.contract_sha256, SHA256, "contract_sha256"),
      confirmed: confirmed(value.confirmed),
    };
  }
  const common = {
    operation,
    channel_partner_id: requestPattern(value.channel_partner_id, ID.partner, "channel_partner_id"),
    expected_revision: requestInteger(value.expected_revision, 1, "expected_revision"),
  };
  if (operation === "update") {
    exact(value, [
      "channel_partner_id", "confirmed", "display_name", "expected_revision", "operation",
      "region_scope", "status", "tier_code",
    ], invalidRequest);
    const updates = {
      display_name: optionalText(value.display_name, 1, 120, "display_name"),
      tier_code: requestOptionalChoice(value.tier_code, TIERS, "tier_code"),
      region_scope: requestOptionalChoice(value.region_scope, REGIONS, "region_scope"),
      status: requestOptionalChoice(value.status, PARTNER_STATUSES, "status"),
    };
    if (Object.values(updates).every((item) => item === null)) invalidRequest("update");
    return { ...common, ...updates, confirmed: confirmed(value.confirmed) };
  }
  if (operation === "upsert_membership") {
    exact(value, [
      "account_id", "channel_partner_id", "confirmed", "expected_revision", "operation", "role_code",
    ], invalidRequest);
    return {
      ...common,
      account_id: requestPattern(value.account_id, ID.account, "account_id"),
      role_code: requestChoice(value.role_code, PARTNER_ROLES, "role_code"),
      confirmed: confirmed(value.confirmed),
    };
  }
  if (operation === "revoke_membership") {
    exact(value, [
      "channel_membership_id", "channel_partner_id", "confirmed", "expected_revision", "operation",
    ], invalidRequest);
    return {
      ...common,
      channel_membership_id: requestPattern(
        value.channel_membership_id, ID.membership, "channel_membership_id",
      ),
      confirmed: confirmed(value.confirmed),
    };
  }
  exact(value, [
    "channel_partner_id", "confirmed", "expected_revision", "expires_at", "operation",
    "referral_code", "usage_limit",
  ], invalidRequest);
  return {
    ...common,
    referral_code: requestPattern(value.referral_code, /^[A-Za-z0-9_-]{20,128}$/u, "referral_code"),
    expires_at: value.expires_at === null ? null : timestamp(value.expires_at, "expires_at", invalidRequest),
    usage_limit: value.usage_limit === null ? null : requestInteger(value.usage_limit, 1, "usage_limit", 1_000_000),
    confirmed: confirmed(value.confirmed),
  };
}

function leadCommand(value) {
  exact(value, [
    "candidate_workspace_id", "channel_partner_id", "confirmed", "contact_email",
    "contact_phone", "expected_revision", "lead_code", "referral_code", "region_code",
    "source_sha256",
  ], invalidRequest);
  const email = optionalText(value.contact_email, 3, 320, "contact_email");
  const phone = requestOptionalPattern(value.contact_phone, /^[+0-9 -]{8,20}$/u, "contact_phone");
  if (email === null && phone === null) invalidRequest("contact");
  return {
    channel_partner_id: requestPattern(value.channel_partner_id, ID.partner, "channel_partner_id"),
    expected_revision: requestInteger(value.expected_revision, 1, "expected_revision"),
    lead_code: requestPattern(value.lead_code, PARTNER_CODE, "lead_code"),
    referral_code: requestOptionalPattern(
      value.referral_code, /^[A-Za-z0-9_-]{20,128}$/u, "referral_code",
    ),
    region_code: requestPattern(value.region_code, /^[a-z0-9][a-z0-9-]{1,15}$/u, "region_code"),
    contact_email: email,
    contact_phone: phone,
    source_sha256: requestPattern(value.source_sha256, SHA256, "source_sha256"),
    candidate_workspace_id: requestOptionalPattern(
      value.candidate_workspace_id, ID.workspace, "candidate_workspace_id",
    ),
    confirmed: confirmed(value.confirmed),
  };
}

function attributionRequest(value) {
  exact(value, [
    "channel_commission_rule_snapshot_id", "channel_lead_id", "channel_partner_id",
    "confirmed", "expected_attribution_revision", "reason_code", "starts_at",
    "target_workspace_id",
  ], invalidRequest);
  return {
    channel_partner_id: requestPattern(value.channel_partner_id, ID.partner, "channel_partner_id"),
    target_workspace_id: requestPattern(value.target_workspace_id, ID.workspace, "target_workspace_id"),
    channel_lead_id: requestOptionalPattern(value.channel_lead_id, ID.lead, "channel_lead_id"),
    channel_commission_rule_snapshot_id: requestPattern(
      value.channel_commission_rule_snapshot_id, ID.rule, "rule_snapshot_id",
    ),
    reason_code: requestPattern(value.reason_code, SAFE_CODE, "reason_code"),
    expected_attribution_revision: requestInteger(
      value.expected_attribution_revision, 0, "expected_attribution_revision",
    ),
    starts_at: timestamp(value.starts_at, "starts_at", invalidRequest),
    confirmed: confirmed(value.confirmed),
  };
}

function attributionApproval(value) {
  exact(value, ["confirmed", "decision", "expected_revision", "reason_code"], invalidRequest);
  return {
    expected_revision: requestInteger(value.expected_revision, 1, "expected_revision"),
    decision: requestChoice(value.decision, ["approved", "rejected"], "decision"),
    reason_code: requestPattern(value.reason_code, SAFE_CODE, "reason_code"),
    confirmed: confirmed(value.confirmed),
  };
}

function partnerSummary(value) {
  exact(value, [
    "channel_partner_id", "commission_rules", "contract_sha256", "created_at", "display_name",
    "memberships", "owner_workspace_id", "partner_code", "pending_attributions", "referral",
    "region_scope", "revision", "status", "tier_code", "updated_at",
  ]);
  array(value.memberships, 500, "memberships");
  array(value.commission_rules, 4, "commission_rules");
  array(value.pending_attributions, 500, "pending_attributions");
  return freeze({
    channel_partner_id: pattern(value.channel_partner_id, ID.partner, "channel_partner_id"),
    owner_workspace_id: pattern(value.owner_workspace_id, ID.workspace, "owner_workspace_id"),
    partner_code: pattern(value.partner_code, PARTNER_CODE, "partner_code"),
    display_name: boundedText(value.display_name, 1, 120, "display_name"),
    tier_code: choice(value.tier_code, TIERS, "tier_code"),
    region_scope: choice(value.region_scope, REGIONS, "region_scope"),
    contract_sha256: pattern(value.contract_sha256, SHA256, "contract_sha256"),
    status: choice(value.status, PARTNER_STATUSES, "status"),
    revision: integer(value.revision, 1, "revision"),
    memberships: value.memberships.map(membershipSummary),
    referral: value.referral === null ? null : referralSummary(value.referral),
    commission_rules: value.commission_rules.map(ruleSummary),
    pending_attributions: value.pending_attributions.map(pendingAttribution),
    created_at: timestamp(value.created_at, "created_at"),
    updated_at: timestamp(value.updated_at, "updated_at"),
  });
}

function membershipSummary(value) {
  exact(value, ["account_id", "channel_membership_id", "revision", "role_code", "status"]);
  return freeze({
    channel_membership_id: pattern(value.channel_membership_id, ID.membership, "membership_id"),
    account_id: pattern(value.account_id, ID.account, "account_id"),
    role_code: choice(value.role_code, PARTNER_ROLES, "role_code"),
    status: choice(value.status, ["active", "revoked"], "membership_status"),
    revision: integer(value.revision, 1, "membership_revision"),
  });
}

function referralSummary(value) {
  exact(value, [
    "channel_referral_code_id", "expires_at", "masked_suffix", "revision", "status",
    "usage_count", "usage_limit",
  ]);
  return freeze({
    channel_referral_code_id: pattern(value.channel_referral_code_id, ID.referral, "referral_id"),
    masked_suffix: pattern(value.masked_suffix, /^[A-Za-z0-9_-]{4}$/u, "masked_suffix"),
    status: choice(value.status, ["active", "revoked", "expired"], "referral_status"),
    usage_limit: value.usage_limit === null ? null : integer(value.usage_limit, 1, "usage_limit"),
    usage_count: integer(value.usage_count, 0, "usage_count"),
    expires_at: value.expires_at === null ? null : timestamp(value.expires_at, "expires_at"),
    revision: integer(value.revision, 1, "referral_revision"),
  });
}

function ruleSummary(value) {
  exact(value, [
    "channel_commission_rule_snapshot_id", "commission_bps", "currency", "effective_from",
    "effective_until", "version",
  ]);
  return freeze({
    channel_commission_rule_snapshot_id: pattern(value.channel_commission_rule_snapshot_id, ID.rule, "rule_id"),
    currency: choice(value.currency, ["CNY", "USD"], "currency"),
    commission_bps: integer(value.commission_bps, 0, "commission_bps", 10_000),
    version: integer(value.version, 1, "rule_version"),
    effective_from: timestamp(value.effective_from, "effective_from"),
    effective_until: value.effective_until === null ? null : timestamp(value.effective_until, "effective_until"),
  });
}

function pendingAttribution(value) {
  exact(value, [
    "approval_count", "approval_generation", "attribution_change_id", "required_approvals",
    "revision", "starts_at", "target_workspace_id", "workspace_attribution_id",
  ]);
  return freeze({
    workspace_attribution_id: pattern(value.workspace_attribution_id, ID.attribution, "attribution_id"),
    attribution_change_id: pattern(value.attribution_change_id, ID.change, "change_id"),
    target_workspace_id: pattern(value.target_workspace_id, ID.workspace, "target_workspace_id"),
    starts_at: timestamp(value.starts_at, "starts_at"),
    required_approvals: integer(value.required_approvals, 1, "required_approvals", 2),
    approval_count: integer(value.approval_count, 0, "approval_count", 2),
    approval_generation: integer(value.approval_generation, 1, "approval_generation"),
    revision: integer(value.revision, 1, "revision"),
  });
}

function leadSummary(value) {
  exact(value, [
    "candidate_workspace_id", "channel_lead_id", "channel_partner_id", "created_at",
    "lead_code", "region_code", "revision", "source_sha256", "stage",
  ]);
  return freeze({
    channel_lead_id: pattern(value.channel_lead_id, ID.lead, "lead_id"),
    channel_partner_id: pattern(value.channel_partner_id, ID.partner, "partner_id"),
    lead_code: pattern(value.lead_code, PARTNER_CODE, "lead_code"),
    region_code: pattern(value.region_code, /^[a-z0-9][a-z0-9-]{1,15}$/u, "region_code"),
    stage: choice(value.stage, [
      "legacy_unresolved", "registered", "qualified", "converted", "closed",
    ], "stage"),
    source_sha256: pattern(value.source_sha256, SHA256, "source_sha256"),
    candidate_workspace_id: optionalPattern(value.candidate_workspace_id, ID.workspace, "candidate_workspace_id"),
    revision: integer(value.revision, 1, "revision"),
    created_at: timestamp(value.created_at, "created_at"),
  });
}

function attributionResponse(value) {
  exact(value, ["attribution", "idempotent_replay", "request_id", "trace_id"]);
  return freeze({
    attribution: attributionSummary(value.attribution),
    idempotent_replay: boolean(value.idempotent_replay, "idempotent_replay"),
    ...trace(value),
  });
}

function attributionSummary(value) {
  exact(value, [
    "approval_count", "approval_generation", "approvals", "attribution_change_id",
    "channel_partner_id", "ends_at", "required_approvals", "revision", "starts_at",
    "status", "target_workspace_id", "workspace_attribution_id",
  ]);
  array(value.approvals, 2, "approvals");
  return freeze({
    workspace_attribution_id: pattern(value.workspace_attribution_id, ID.attribution, "attribution_id"),
    attribution_change_id: pattern(value.attribution_change_id, ID.change, "change_id"),
    channel_partner_id: pattern(value.channel_partner_id, ID.partner, "partner_id"),
    target_workspace_id: pattern(value.target_workspace_id, ID.workspace, "workspace_id"),
    status: choice(value.status, [
      "pending_approval", "active", "superseded", "rejected", "cancelled", "expired",
    ], "attribution_status"),
    starts_at: timestamp(value.starts_at, "starts_at"),
    ends_at: value.ends_at === null ? null : timestamp(value.ends_at, "ends_at"),
    required_approvals: integer(value.required_approvals, 1, "required_approvals", 2),
    approval_count: integer(value.approval_count, 0, "approval_count", 2),
    approval_generation: integer(value.approval_generation, 1, "approval_generation"),
    revision: integer(value.revision, 1, "revision"),
    approvals: value.approvals.map(approvalSummary),
  });
}

function approvalSummary(value) {
  exact(value, [
    "attribution_change_approval_id", "approver_operator_id", "decided_at", "decision",
    "reason_code",
  ]);
  return freeze({
    attribution_change_approval_id: pattern(value.attribution_change_approval_id, ID.approval, "approval_id"),
    approver_operator_id: pattern(value.approver_operator_id, ID.operator, "operator_id"),
    decision: choice(value.decision, ["approved", "rejected"], "decision"),
    reason_code: pattern(value.reason_code, SAFE_CODE, "reason_code"),
    decided_at: timestamp(value.decided_at, "decided_at"),
  });
}

function statementSummary(value) {
  exact(value, [
    "channel_partner_id", "channel_statement_id", "close_revision", "closed_at", "currency",
    "line_count", "period_end", "period_start", "reconciliation_sha256", "status",
    "total_amount_micros", "total_commission_micros",
  ]);
  return freeze({
    channel_statement_id: pattern(value.channel_statement_id, ID.statement, "statement_id"),
    channel_partner_id: pattern(value.channel_partner_id, ID.partner, "partner_id"),
    period_start: timestamp(value.period_start, "period_start"),
    period_end: timestamp(value.period_end, "period_end"),
    currency: choice(value.currency, ["CNY", "USD"], "currency"),
    total_amount_micros: integer(value.total_amount_micros, 0, "total_amount_micros"),
    total_commission_micros: integer(value.total_commission_micros, 0, "commission_micros"),
    line_count: integer(value.line_count, 0, "line_count"),
    reconciliation_sha256: pattern(value.reconciliation_sha256, SHA256, "reconciliation_sha256"),
    status: choice(value.status, ["imported", "open", "closed"], "statement_status"),
    close_revision: integer(value.close_revision, 0, "close_revision"),
    closed_at: value.closed_at === null ? null : timestamp(value.closed_at, "closed_at"),
  });
}

async function post(controlApiUrl, path, options) {
  return requestJson(controlApiUrl, path, {
    method: "POST",
    body: options.body,
    revision: requestInteger(options.revision, 0, "revision"),
    stepUpGrant: requestPattern(options.stepUpGrant, OPAQUE, "step_up_grant"),
    idempotencyKey: requestPattern(options.idempotencyKey, IDEMPOTENCY, "idempotency_key"),
    signal: options.signal,
  });
}

async function requestJson(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.method === "POST") Object.assign(headers, {
    "Content-Type": "application/json",
    "Idempotency-Key": options.idempotencyKey,
    "If-Match": `"${options.revision}"`,
    "X-CSRF-Token": readPlatformCsrfCookie(),
    "X-Step-Up-Grant": options.stepUpGrant,
  });
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new PlatformApiError({ status: 0, code: "channel.unavailable", message: "Channel service is unavailable" });
  }
  const payload = await readJson(response);
  if (!response.ok) throw serverError(response, payload);
  privateNoStore(response);
  return payload;
}

async function readJson(response) {
  if (!/^application\/json(?:\s*;|$)/iu.test(response.headers.get("Content-Type") ?? "")) {
    invalidResponse("content_type");
  }
  try {
    const value = await response.json();
    plain(value, "payload");
    return value;
  } catch (error) {
    if (error instanceof PlatformApiError) throw error;
    invalidResponse("json");
  }
}

function serverError(response, payload) {
  const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(payload?.code ?? ""))
    ? payload.code : "channel.request_invalid";
  return new PlatformApiError({
    status: response.status, code, message: safeText(payload?.message) || "Request failed",
    requestId: safeText(payload?.request_id) || "-",
  });
}

function privateNoStore(response) {
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  if (!directives.includes("private") || !directives.includes("no-store")) {
    invalidResponse("cache_control");
  }
}

function apiUrl(baseUrl, path) {
  let base;
  try { base = new URL(String(baseUrl ?? "")); } catch { invalidRequest("control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password
    || base.search || base.hash || !new Set(["", "/"]).has(base.pathname)) {
    invalidRequest("control_api_url");
  }
  return new URL(path, base).toString();
}

function pagination(limit, offset) {
  return new URLSearchParams({
    limit: String(requestInteger(Number(limit), 1, "limit", 100)),
    offset: String(requestInteger(Number(offset), 0, "offset", 1_000_000)),
  });
}

function optionalChoice(query, key, value, choices) {
  const text = String(value ?? "");
  if (!text) return;
  query.set(key, requestChoice(text, choices, key));
}

function exact(value, keys, fail = invalidResponse) {
  plain(value, "object", fail);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail("keys");
  }
}

function plain(value, label, fail = invalidResponse) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) fail(label);
}

function array(value, maximum, label) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(label);
}

function pattern(value, regex, label, fail = invalidResponse) {
  if (typeof value !== "string" || !regex.test(value)) fail(label);
  return value;
}

function optionalPattern(value, regex, label, fail = invalidResponse) {
  return value === null || value === undefined || value === ""
    ? null : pattern(value, regex, label, fail);
}

function choice(value, choices, label, fail = invalidResponse) {
  if (!choices.includes(value)) fail(label);
  return value;
}

function optionalChoiceValue(value, choices, label, fail = invalidResponse) {
  return value === null || value === undefined || value === ""
    ? null : choice(value, choices, label, fail);
}

function integer(
  value, minimum, label, maximum = Number.MAX_SAFE_INTEGER, fail = invalidResponse,
) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) fail(label);
  return value;
}

function boolean(value, label) {
  if (typeof value !== "boolean") invalidResponse(label);
  return value;
}

function confirmed(value) {
  if (value !== true) invalidRequest("confirmed");
  return true;
}

function boundedText(value, minimum, maximum, label, fail = invalidResponse) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum
    || /[\u0000-\u001f\u007f]/u.test(value)) fail(label);
  return value;
}

function optionalText(value, minimum, maximum, label) {
  return value === null || value === undefined || value === ""
    ? null : boundedText(value, minimum, maximum, label, invalidRequest);
}

function requestPattern(value, regex, label) {
  return pattern(value, regex, label, invalidRequest);
}

function requestOptionalPattern(value, regex, label) {
  return optionalPattern(value, regex, label, invalidRequest);
}

function requestChoice(value, choices, label) {
  return choice(value, choices, label, invalidRequest);
}

function requestOptionalChoice(value, choices, label) {
  return optionalChoiceValue(value, choices, label, invalidRequest);
}

function requestInteger(value, minimum, label, maximum = Number.MAX_SAFE_INTEGER) {
  return integer(value, minimum, label, maximum, invalidRequest);
}

function timestamp(value, label, fail = invalidResponse) {
  if (typeof value !== "string" || value.length < 20 || value.length > 40
    || !Number.isFinite(Date.parse(value)) || !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value)) {
    fail(label);
  }
  return value;
}

function trace(value) {
  return {
    request_id: boundedText(value.request_id, 1, 160, "request_id"),
    trace_id: boundedText(value.trace_id, 1, 160, "trace_id"),
  };
}

function freeze(value) {
  for (const item of Object.values(value)) if (Array.isArray(item)) Object.freeze(item);
  return Object.freeze(value);
}

function invalidRequest(label) {
  throw new PlatformApiError({ status: 400, code: "channel.request_invalid", message: `Invalid ${label}` });
}

function invalidResponse(label) {
  throw new PlatformApiError({ status: 502, code: "channel.response_invalid", message: `Invalid ${label}` });
}

function safeText(value) {
  return typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240) : "";
}
