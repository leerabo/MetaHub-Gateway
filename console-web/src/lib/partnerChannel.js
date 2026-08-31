/** Isolated PRD-139 Partner read client. This module has no Platform, Workspace, or Support imports. */

const ID = Object.freeze({
  account: /^gac_[0-9a-f]{32}$/u,
  membership: /^chm_[0-9a-f]{32}$/u,
  partner: /^par_[0-9a-f]{32}$/u,
  referral: /^crc_[0-9a-f]{32}$/u,
  workspace: /^wsp_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const PARTNER_CODE = /^[a-z0-9][a-z0-9._-]{0,79}$/u;
const ROLES = ["partner_owner", "partner_operations", "partner_finance"];

export class PartnerApiError extends Error {
  constructor({ status = 0, code = "partner.unavailable", message = "Partner service is unavailable", requestId = "-" } = {}) {
    super(safeText(message) || "Partner service is unavailable");
    this.name = "PartnerApiError";
    this.status = Number.isSafeInteger(status) ? status : 0;
    this.code = safeText(code) || "partner.unavailable";
    this.requestId = safeText(requestId) || "-";
  }
}

export async function listPartnerWorkspaces({ controlApiUrl, limit = 50, offset = 0, signal } = {}) {
  const query = new URLSearchParams({
    limit: String(integer(Number(limit), 1, "limit", 100, invalidRequest)),
    offset: String(integer(Number(offset), 0, "offset", 1_000_000, invalidRequest)),
  });
  const value = await requestJson(controlApiUrl, `/partner/workspaces?${query.toString()}`, signal);
  exact(value, ["items", "limit", "offset", "partner", "request_id", "role_code", "total", "trace_id"]);
  array(value.items, 100, "items");
  const partner = partnerSummary(value.partner);
  if (partner.commission_rules.length || partner.pending_attributions.length) invalidResponse("partner_projection");
  return Object.freeze({
    partner,
    role_code: choice(value.role_code, ROLES, "role_code"),
    items: Object.freeze(value.items.map(workspaceSummary)),
    total: integer(value.total, 0, "total"),
    limit: integer(value.limit, 1, "limit", 100),
    offset: integer(value.offset, 0, "offset", 1_000_000),
    request_id: text(value.request_id, 1, 160, "request_id"),
    trace_id: text(value.trace_id, 1, 160, "trace_id"),
  });
}

async function requestJson(controlApiUrl, path, signal) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new PartnerApiError();
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new PartnerApiError({
      status: response.status,
      code: /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(payload?.code ?? ""))
        ? payload.code : "partner.request_invalid",
      message: payload?.message,
      requestId: payload?.request_id,
    });
  }
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
    if (error instanceof PartnerApiError) throw error;
    invalidResponse("json");
  }
}

function partnerSummary(value) {
  exact(value, [
    "channel_partner_id", "commission_rules", "contract_sha256", "created_at", "display_name",
    "memberships", "owner_workspace_id", "partner_code", "pending_attributions", "referral",
    "region_scope", "revision", "status", "tier_code", "updated_at",
  ]);
  array(value.memberships, 1, "memberships");
  array(value.commission_rules, 0, "commission_rules");
  array(value.pending_attributions, 0, "pending_attributions");
  return Object.freeze({
    channel_partner_id: pattern(value.channel_partner_id, ID.partner, "partner_id"),
    owner_workspace_id: pattern(value.owner_workspace_id, ID.workspace, "owner_workspace_id"),
    partner_code: pattern(value.partner_code, PARTNER_CODE, "partner_code"),
    display_name: text(value.display_name, 1, 120, "display_name"),
    tier_code: choice(value.tier_code, ["registered", "silver", "gold", "strategic"], "tier_code"),
    region_scope: choice(value.region_scope, ["global", "cn", "apac", "emea", "americas"], "region_scope"),
    contract_sha256: pattern(value.contract_sha256, SHA256, "contract_sha256"),
    status: choice(value.status, ["active", "suspended", "retired"], "status"),
    revision: integer(value.revision, 1, "revision"),
    memberships: Object.freeze(value.memberships.map(membershipSummary)),
    referral: value.referral === null ? null : referralSummary(value.referral),
    commission_rules: Object.freeze([]),
    pending_attributions: Object.freeze([]),
    created_at: timestamp(value.created_at, "created_at"),
    updated_at: timestamp(value.updated_at, "updated_at"),
  });
}

function membershipSummary(value) {
  exact(value, ["account_id", "channel_membership_id", "revision", "role_code", "status"]);
  return Object.freeze({
    channel_membership_id: pattern(value.channel_membership_id, ID.membership, "membership_id"),
    account_id: pattern(value.account_id, ID.account, "account_id"),
    role_code: choice(value.role_code, ROLES, "role_code"),
    status: choice(value.status, ["active", "revoked"], "membership_status"),
    revision: integer(value.revision, 1, "membership_revision"),
  });
}

function referralSummary(value) {
  exact(value, [
    "channel_referral_code_id", "expires_at", "masked_suffix", "revision", "status",
    "usage_count", "usage_limit",
  ]);
  return Object.freeze({
    channel_referral_code_id: pattern(value.channel_referral_code_id, ID.referral, "referral_id"),
    masked_suffix: pattern(value.masked_suffix, /^[A-Za-z0-9_-]{4}$/u, "masked_suffix"),
    status: choice(value.status, ["active", "revoked", "expired"], "referral_status"),
    usage_limit: value.usage_limit === null ? null : integer(value.usage_limit, 1, "usage_limit"),
    usage_count: integer(value.usage_count, 0, "usage_count"),
    expires_at: value.expires_at === null ? null : timestamp(value.expires_at, "expires_at"),
    revision: integer(value.revision, 1, "referral_revision"),
  });
}

function workspaceSummary(value) {
  exact(value, [
    "attribution_starts_at", "display_name", "operational_status", "region_code",
    "statement_totals", "workspace_id",
  ]);
  let totals = null;
  if (value.statement_totals !== null) {
    plain(value.statement_totals, "statement_totals");
    const keys = Object.keys(value.statement_totals);
    if (keys.some((key) => !["CNY", "USD"].includes(key))) invalidResponse("statement_currency");
    totals = Object.freeze(Object.fromEntries(keys.map((key) => [
      key, integer(value.statement_totals[key], 0, "statement_total"),
    ])));
  }
  return Object.freeze({
    workspace_id: pattern(value.workspace_id, ID.workspace, "workspace_id"),
    display_name: text(value.display_name, 1, 120, "display_name"),
    region_code: pattern(value.region_code, /^[a-z0-9][a-z0-9-]{1,15}$/u, "region_code"),
    attribution_starts_at: timestamp(value.attribution_starts_at, "attribution_starts_at"),
    operational_status: choice(value.operational_status, [
      "active", "suspended", "deletion_pending", "deleted", "migration_conflict",
    ], "operational_status"),
    statement_totals: totals,
  });
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

function privateNoStore(response) {
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  if (!directives.includes("private") || !directives.includes("no-store")) invalidResponse("cache_control");
}

function exact(value, keys) {
  plain(value, "object");
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    invalidResponse("keys");
  }
}

function plain(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse(label);
}

function array(value, maximum, label) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(label);
}

function pattern(value, regex, label) {
  if (typeof value !== "string" || !regex.test(value)) invalidResponse(label);
  return value;
}

function text(value, minimum, maximum, label) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum
    || /[\u0000-\u001f\u007f]/u.test(value)) invalidResponse(label);
  return value;
}

function choice(value, choices, label) {
  if (!choices.includes(value)) invalidResponse(label);
  return value;
}

function integer(value, minimum, label, maximum = Number.MAX_SAFE_INTEGER, fail = invalidResponse) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) fail(label);
  return value;
}

function timestamp(value, label) {
  if (typeof value !== "string" || value.length < 20 || value.length > 40
    || !Number.isFinite(Date.parse(value)) || !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value)) {
    invalidResponse(label);
  }
  return value;
}

function invalidRequest(label) {
  throw new PartnerApiError({ status: 400, code: "partner.request_invalid", message: `Invalid ${label}` });
}

function invalidResponse(label) {
  throw new PartnerApiError({ status: 502, code: "partner.response_invalid", message: `Invalid ${label}` });
}

function safeText(value) {
  return typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240) : "";
}
