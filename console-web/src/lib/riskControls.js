import { readIdentityCsrfCookie } from "./identity.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const CASE_ID = /^rca_[0-9a-f]{32}$/u;
const CHALLENGE_ID = /^rch_[0-9a-f]{32}$/u;
const RESTRICTION_ID = /^rst_[0-9a-f]{32}$/u;
const APPEAL_ID = /^rap_[0-9a-f]{32}$/u;
const DECISION_ID = /^rdc_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const CASE_STATUSES = new Set(["open", "under_review", "restricted", "cleared", "closed"]);
const ERROR_MESSAGES = Object.freeze({
  "platform.permission_denied": "The current platform role cannot perform this action.",
  "risk.request_invalid": "The risk-control request is invalid.",
  "risk.case_not_found": "The risk case is not available.",
  "risk.restriction_not_found": "The restriction is not available.",
  "risk.challenge_not_found": "The challenge is not available.",
  "risk.challenge_invalid": "The challenge proof is invalid.",
  "risk.challenge_expired": "The challenge has expired.",
  "risk.challenge_replayed": "The challenge has already been consumed.",
  "risk.revision_conflict": "The risk case changed. Refresh before retrying.",
  "risk.idempotency_conflict": "The idempotency key is bound to another action.",
  "risk.cursor_invalid": "The risk-case cursor is invalid.",
  "risk.unavailable": "Risk controls are temporarily unavailable.",
  "risk.response_invalid": "The risk-control response is invalid.",
});

export function listRiskCases({ controlApiUrl, status = "", limit = 50, cursor = null, signal } = {}) {
  if ((status && !CASE_STATUSES.has(status)) || !integer(Number(limit), 1, 100)
    || (cursor !== null && (typeof cursor !== "string" || cursor.length > 2048))) {
    invalidRequest();
  }
  const query = new URLSearchParams({ limit: String(Number(limit)) });
  if (status) query.set("status", status);
  if (cursor) query.set("cursor", cursor);
  return request(controlApiUrl, `/platform/risk/cases?${query}`, { signal }, validCaseList);
}

export function decideRiskCase({
  controlApiUrl,
  caseId,
  revision,
  decision,
  reasonCode,
  restrictionKind = null,
  restrictionMinutes = null,
  idempotencyKey,
  signal,
} = {}) {
  const payload = {
    decision,
    reason_code: reasonCode,
    restriction_kind: restrictionKind,
    restriction_minutes: restrictionMinutes,
    confirmed: true,
  };
  if (!CASE_ID.test(String(caseId ?? "")) || !integer(Number(revision), 1)
    || !IDEMPOTENCY_KEY.test(String(idempotencyKey ?? "")) || !validDecisionRequest(payload)) {
    invalidRequest();
  }
  return request(
    controlApiUrl,
    `/platform/risk/cases/${encodeURIComponent(caseId)}/decisions`,
    writeOptions(readPlatformCsrfCookie(), idempotencyKey, payload, signal, revision),
    validDecisionResponse,
  );
}

export function completeRiskChallenge({
  controlApiUrl,
  challengeId,
  challengeToken,
  answer,
  revision,
  credential = "global",
  idempotencyKey,
  signal,
} = {}) {
  if (!CHALLENGE_ID.test(String(challengeId ?? ""))
    || typeof challengeToken !== "string" || challengeToken.length < 32
    || challengeToken.length > 256 || typeof answer !== "string"
    || answer.length < 6 || answer.length > 128 || !integer(Number(revision), 1)
    || !new Set(["global", "workspace"]).has(credential)
    || !IDEMPOTENCY_KEY.test(String(idempotencyKey ?? ""))) invalidRequest();
  const csrf = credential === "workspace"
    ? readWorkspaceCsrfCookie()
    : readIdentityCsrfCookie();
  return request(
    controlApiUrl,
    "/risk/challenges",
    writeOptions(csrf, idempotencyKey, {
      challenge_id: challengeId,
      challenge_token: challengeToken,
      answer,
      revision: Number(revision),
      confirmed: true,
    }, signal),
    validChallengeResponse,
  );
}

export function createRiskAppeal({
  controlApiUrl,
  restrictionId,
  category,
  idempotencyKey,
  signal,
} = {}) {
  if (!RESTRICTION_ID.test(String(restrictionId ?? ""))
    || !new Set(["not_my_activity", "expected_usage", "restriction_error", "other"])
      .has(category)
    || !IDEMPOTENCY_KEY.test(String(idempotencyKey ?? ""))) invalidRequest();
  return request(
    controlApiUrl,
    "/risk/appeals",
    writeOptions(readWorkspaceCsrfCookie(), idempotencyKey, {
      restriction_id: restrictionId,
      category,
      confirmed: true,
    }, signal),
    validAppealResponse,
  );
}

function writeOptions(csrf, idempotencyKey, payload, signal, revision = null) {
  const headers = {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrf,
    "Idempotency-Key": idempotencyKey,
  };
  if (revision !== null) headers["If-Match"] = `"${Number(revision)}"`;
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
    throw clientError("risk.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = typeof payload.code === "string" && ERROR_MESSAGES[payload.code]
      ? payload.code : "risk.unavailable";
    throw clientError(
      code,
      response.status,
      LINK_ID.test(String(payload.request_id ?? "")) ? payload.request_id : "-",
    );
  }
  if (!validator(payload)) throw clientError("risk.response_invalid", 502);
  return payload;
}

function validCaseList(value) {
  return plainObject(value) && exactKeys(value, [
    "items", "total", "limit", "next_cursor", "projection", "request_id", "trace_id",
  ]) && Array.isArray(value.items) && value.items.length <= 100
    && value.items.every(validCase) && integer(value.total, 0)
    && integer(value.limit, 1, 100)
    && (value.next_cursor === null || (typeof value.next_cursor === "string"
      && value.next_cursor.length <= 2048))
    && new Set(["security", "support"]).has(value.projection) && links(value);
}

function validCase(value) {
  return plainObject(value) && exactKeys(value, [
    "risk_case_id", "workspace_id", "case_kind", "safe_reason_family", "score_band",
    "status", "signal_families", "restriction", "appeals", "revision", "opened_at",
    "updated_at",
  ]) && CASE_ID.test(String(value.risk_case_id))
    && WORKSPACE_ID.test(String(value.workspace_id))
    && new Set(["credential_stuffing", "resource_abuse", "api_abuse", "account_takeover"])
      .has(value.case_kind)
    && new Set(["unusual_volume", "repeated_failures", "automation_detected",
      "resource_spike", "manual_review"]).has(value.safe_reason_family)
    && (value.score_band === null || new Set(["low", "medium", "high", "critical"])
      .has(value.score_band))
    && CASE_STATUSES.has(value.status) && uniqueStrings(value.signal_families, 0, 20)
    && (value.restriction === null || validRestriction(value.restriction))
    && Array.isArray(value.appeals) && value.appeals.length <= 100
    && value.appeals.every(validAppeal) && integer(value.revision, 1)
    && timestamp(value.opened_at) && timestamp(value.updated_at);
}

function validRestriction(value) {
  return plainObject(value) && exactKeys(value, [
    "risk_restriction_id", "restriction_kind", "status", "starts_at", "expires_at",
    "cleared_at", "revision",
  ]) && RESTRICTION_ID.test(String(value.risk_restriction_id))
    && new Set(["api_calls", "resource_creation", "workspace_access"])
      .has(value.restriction_kind)
    && new Set(["active", "cleared", "expired"]).has(value.status)
    && timestamp(value.starts_at) && timestamp(value.expires_at)
    && (value.cleared_at === null || timestamp(value.cleared_at))
    && integer(value.revision, 1);
}

function validAppeal(value) {
  return plainObject(value) && exactKeys(value, [
    "risk_appeal_id", "category", "status", "resolution_reason", "revision", "created_at",
    "resolved_at",
  ]) && APPEAL_ID.test(String(value.risk_appeal_id))
    && new Set(["not_my_activity", "expected_usage", "restriction_error", "other"])
      .has(value.category)
    && new Set(["pending", "approved", "rejected"]).has(value.status)
    && (value.resolution_reason === null || new Set(["verified_false_positive",
      "restriction_confirmed", "insufficient_evidence"]).has(value.resolution_reason))
    && integer(value.revision, 1) && timestamp(value.created_at)
    && (value.resolved_at === null || timestamp(value.resolved_at));
}

function validDecisionRequest(value) {
  if (!plainObject(value) || !exactKeys(value, [
    "decision", "reason_code", "restriction_kind", "restriction_minutes", "confirmed",
  ]) || value.confirmed !== true) return false;
  if (value.decision === "required_review") {
    return value.reason_code === "manual_review" && value.restriction_kind === null
      && value.restriction_minutes === null;
  }
  if (value.decision === "restricted") {
    return value.reason_code === "automation_confirmed"
      && new Set(["api_calls", "resource_creation", "workspace_access"])
        .has(value.restriction_kind)
      && integer(value.restriction_minutes, 5, 10080);
  }
  return value.decision === "cleared"
    && new Set(["false_positive", "appeal_approved"]).has(value.reason_code)
    && value.restriction_kind === null && value.restriction_minutes === null;
}

function validDecisionResponse(value) {
  const decision = value?.decision;
  return plainObject(value) && exactKeys(value, [
    "case", "decision", "idempotent_replay", "request_id", "trace_id",
  ]) && validCase(value.case) && plainObject(decision) && exactKeys(decision, [
    "risk_decision_id", "risk_case_id", "decision", "safe_reason_family", "policy_version",
    "expires_at", "decided_at",
  ]) && DECISION_ID.test(String(decision.risk_decision_id))
    && CASE_ID.test(String(decision.risk_case_id))
    && new Set(["allow", "challenge", "required_review", "restricted", "cleared"])
      .has(decision.decision)
    && typeof decision.safe_reason_family === "string"
    && decision.safe_reason_family.length >= 3 && decision.safe_reason_family.length <= 64
    && integer(decision.policy_version, 1)
    && (decision.expires_at === null || timestamp(decision.expires_at))
    && timestamp(decision.decided_at) && typeof value.idempotent_replay === "boolean"
    && links(value);
}

function validChallengeResponse(value) {
  const challenge = value?.challenge;
  return plainObject(value) && exactKeys(value, [
    "challenge", "completed", "idempotent_replay", "request_id", "trace_id",
  ]) && plainObject(challenge) && exactKeys(challenge, [
    "risk_challenge_id", "challenge_kind", "status", "attempts", "max_attempts",
    "expires_at", "revision",
  ]) && CHALLENGE_ID.test(String(challenge.risk_challenge_id))
    && new Set(["email_otp", "totp", "proof_of_control"]).has(challenge.challenge_kind)
    && new Set(["pending", "consumed", "failed", "expired"]).has(challenge.status)
    && integer(challenge.attempts, 0, 5) && integer(challenge.max_attempts, 1, 5)
    && timestamp(challenge.expires_at) && integer(challenge.revision, 1)
    && value.completed === true && typeof value.idempotent_replay === "boolean"
    && links(value);
}

function validAppealResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "appeal", "idempotent_replay", "request_id", "trace_id",
  ]) && validAppeal(value.appeal) && typeof value.idempotent_replay === "boolean"
    && links(value);
}

function apiUrl(controlApiUrl, path) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

function invalidRequest() { throw clientError("risk.request_invalid", 0); }
function clientError(code, status, requestId = "-") {
  return new PlatformApiError({
    code,
    status,
    requestId,
    message: ERROR_MESSAGES[code] ?? "Risk-control request failed.",
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
    && new Set(value).size === value.length
    && value.every((item) => typeof item === "string" && item.length <= 64);
}
function timestamp(value) {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value));
}
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
