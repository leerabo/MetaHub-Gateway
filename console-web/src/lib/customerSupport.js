/** PRD-132 strict Cookie-only client shared by Workspace and Platform support consoles. */

import { AuthApiError } from "./auth.js";

const WORKSPACE_CSRF_COOKIE = "__Host-metahub-workspace-csrf";
const PLATFORM_CSRF_COOKIE = "__Host-metahub-platform-csrf";
const DOMAINS = new Set(["workspace", "platform"]);
const CATEGORIES = new Set(["account", "billing", "integration", "migration", "reliability", "security"]);
const SEVERITIES = new Set(["p1", "p2", "p3", "p4"]);
const STATUSES = new Set(["open", "triaged", "waiting_customer", "waiting_platform", "resolved", "closed"]);
const VISIBILITIES = new Set(["customer", "internal"]);
const RELATED_KINDS = new Set(["none", "request", "incident", "edge_deployment", "migration"]);
const ATTACHMENT_INTENTS = new Set(["diagnostic_evidence", "log_bundle", "reproduction"]);
const TICKET_ID = /^tkt_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const OPERATOR_ID = /^pto_[0-9a-f]{32}$/u;
const FILE_ID = /^file_[0-9a-f]{32}$/u;
const REASON = /^[a-z0-9][a-z0-9._-]{2,79}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const SAFE_CHAIN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;

export async function listSupportTickets({
  controlApiUrl,
  actorDomain,
  limit = 50,
  status = "",
  severity = "",
  workspaceId = "",
  signal,
} = {}) {
  const domain = requireDomain(actorDomain);
  const query = new URLSearchParams();
  query.set("limit", String(integer(limit, 1, 100)));
  if (status) query.set("status", choice(status, STATUSES));
  if (severity) query.set("severity", choice(severity, SEVERITIES));
  if (workspaceId) {
    if (domain !== "platform" || !WORKSPACE_ID.test(String(workspaceId))) invalidRequest();
    query.set("workspace_id", String(workspaceId));
  }
  return supportRequest(controlApiUrl, `/support/tickets?${query}`, {
    actorDomain: domain,
    responseType: "list",
    signal,
  });
}

export async function createSupportTicket({
  controlApiUrl,
  category,
  severity,
  subject,
  message,
  relatedKind = "none",
  relatedReference = null,
  idempotencyKey,
  signal,
} = {}) {
  const kind = choice(relatedKind, RELATED_KINDS);
  const reference = optionalBoundedText(relatedReference, 3, 256);
  if ((kind === "none") !== (reference === null)) invalidRequest();
  return supportRequest(controlApiUrl, "/support/tickets", {
    actorDomain: "workspace",
    method: "POST",
    csrf: true,
    headers: { "Idempotency-Key": requireIdempotency(idempotencyKey) },
    body: {
      category: choice(category, CATEGORIES),
      severity: choice(severity, SEVERITIES),
      subject: boundedText(subject, 3, 200),
      message: boundedText(message, 1, 8000),
      related_kind: kind,
      related_reference: reference,
    },
    responseType: "ticketMutation",
    signal,
  });
}

export async function patchSupportTicket({
  controlApiUrl,
  actorDomain,
  ticketId,
  expectedRevision,
  changes,
  idempotencyKey,
  signal,
} = {}) {
  const domain = requireDomain(actorDomain);
  return supportRequest(controlApiUrl, `/support/tickets/${encodeURIComponent(ticket(ticketId))}`, {
    actorDomain: domain,
    method: "PATCH",
    csrf: true,
    headers: {
      "If-Match": `"${integer(expectedRevision, 1, Number.MAX_SAFE_INTEGER)}"`,
      "Idempotency-Key": requireIdempotency(idempotencyKey),
    },
    body: patchPayload(changes, domain),
    responseType: "ticketMutation",
    signal,
  });
}

export async function attachSupportFile({
  controlApiUrl,
  ticketId,
  expectedRevision,
  fileId,
  intentCode,
  idempotencyKey,
  signal,
} = {}) {
  if (!FILE_ID.test(String(fileId ?? ""))) invalidRequest();
  return supportRequest(
    controlApiUrl,
    `/support/tickets/${encodeURIComponent(ticket(ticketId))}/attachments`,
    {
      actorDomain: "workspace",
      method: "POST",
      csrf: true,
      headers: {
        "If-Match": `"${integer(expectedRevision, 1, Number.MAX_SAFE_INTEGER)}"`,
        "Idempotency-Key": requireIdempotency(idempotencyKey),
      },
      body: {
        file_id: String(fileId),
        intent_code: choice(intentCode, ATTACHMENT_INTENTS),
        confirmed: true,
      },
      responseType: "attachmentMutation",
      signal,
    },
  );
}

export async function escalateSupportTicket({
  controlApiUrl,
  actorDomain,
  ticketId,
  expectedRevision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  return supportRequest(
    controlApiUrl,
    `/support/tickets/${encodeURIComponent(ticket(ticketId))}/escalations`,
    {
      actorDomain: requireDomain(actorDomain),
      method: "POST",
      csrf: true,
      headers: {
        "If-Match": `"${integer(expectedRevision, 1, Number.MAX_SAFE_INTEGER)}"`,
        "Idempotency-Key": requireIdempotency(idempotencyKey),
      },
      body: { reason_code: reason(reasonCode), confirmed: true },
      responseType: "escalationMutation",
      signal,
    },
  );
}

export function createSupportIdempotencyKey(scope = "command") {
  const normalized = String(scope).replace(/[^a-z0-9_-]/giu, "").slice(0, 24) || "command";
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    ?? `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `support-${normalized}-${random}`.slice(0, 128);
}

async function supportRequest(controlApiUrl, path, options) {
  const headers = { Accept: "application/json", ...(options.headers ?? {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) {
    const cookieName = options.actorDomain === "workspace"
      ? WORKSPACE_CSRF_COOKIE : PLATFORM_CSRF_COOKIE;
    const csrf = readCookie(cookieName);
    if (!csrf) invalidRequest("support.csrf_missing");
    headers["X-CSRF-Token"] = csrf;
  }
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
    throw new AuthApiError({ status: 0, code: "support.network_unavailable", message: "暂时无法连接支持服务。" });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new AuthApiError({
      status: response.status,
      code: safeCode(payload?.code),
      message: typeof payload?.message === "string" ? payload.message : "支持请求失败。",
      requestId: safeChain(payload?.request_id),
      traceId: safeChain(payload?.trace_id),
    });
  }
  try {
    return validateResponse(payload, options.responseType, options.actorDomain);
  } catch {
    throw new AuthApiError({
      status: response.status,
      code: "support.response_invalid",
      message: "支持服务返回了无效响应。",
      requestId: safeChain(payload?.request_id),
      traceId: safeChain(payload?.trace_id),
    });
  }
}

function validateResponse(payload, type, actorDomain) {
  if (type === "list") {
    exact(payload, ["actor_domain", "items", "request_id", "trace_id"]);
    if (payload.actor_domain !== actorDomain || !Array.isArray(payload.items)) throw new TypeError();
    payload.items.forEach(validateTicket);
  } else if (type === "ticketMutation") {
    exact(payload, ["actor_domain", "ticket", "idempotent_replay", "request_id", "trace_id"]);
    if (payload.actor_domain !== actorDomain || typeof payload.idempotent_replay !== "boolean") throw new TypeError();
    validateTicket(payload.ticket);
  } else if (type === "attachmentMutation") {
    exact(payload, ["attachment", "ticket_revision", "idempotent_replay", "request_id", "trace_id"]);
    validateAttachment(payload.attachment);
    positive(payload.ticket_revision);
    if (typeof payload.idempotent_replay !== "boolean") throw new TypeError();
  } else if (type === "escalationMutation") {
    exact(payload, ["escalation", "ticket_revision", "idempotent_replay", "request_id", "trace_id"]);
    validateEscalation(payload.escalation);
    positive(payload.ticket_revision);
    if (typeof payload.idempotent_replay !== "boolean") throw new TypeError();
  } else {
    throw new TypeError();
  }
  chain(payload.request_id);
  chain(payload.trace_id);
  return deepFreeze(payload);
}

function validateTicket(value) {
  exact(value, [
    "ticket_id", "workspace_id", "category", "severity", "status", "subject",
    "reported_by_me", "assignee_operator_id", "revision", "last_customer_activity_at",
    "last_platform_activity_at", "resolved_at", "closed_at", "created_at", "updated_at",
    "messages", "attachments", "sla", "escalations", "survey",
  ]);
  if (!TICKET_ID.test(value.ticket_id) || !WORKSPACE_ID.test(value.workspace_id)
    || !CATEGORIES.has(value.category) || !SEVERITIES.has(value.severity)
    || !STATUSES.has(value.status) || typeof value.subject !== "string"
    || value.subject.length < 3 || value.subject.length > 200
    || typeof value.reported_by_me !== "boolean"
    || (value.assignee_operator_id !== null && !OPERATOR_ID.test(value.assignee_operator_id))) throw new TypeError();
  positive(value.revision);
  timestamp(value.last_customer_activity_at);
  nullableTimestamp(value.last_platform_activity_at);
  nullableTimestamp(value.resolved_at);
  nullableTimestamp(value.closed_at);
  timestamp(value.created_at);
  timestamp(value.updated_at);
  if (!Array.isArray(value.messages) || !Array.isArray(value.attachments)
    || !Array.isArray(value.escalations)) throw new TypeError();
  value.messages.forEach(validateMessage);
  value.attachments.forEach(validateAttachment);
  value.escalations.forEach(validateEscalation);
  validateSla(value.sla);
  if (value.survey !== null) validateSurvey(value.survey);
}

function validateMessage(value) {
  exact(value, ["message_id", "author_domain", "visibility", "body", "created_at"]);
  if (!/^msg_[0-9a-f]{32}$/u.test(value.message_id)
    || !new Set(["customer", "platform"]).has(value.author_domain)
    || !VISIBILITIES.has(value.visibility) || typeof value.body !== "string"
    || value.body.length < 1 || value.body.length > 8000) throw new TypeError();
  timestamp(value.created_at);
}

function validateAttachment(value) {
  exact(value, [
    "attachment_id", "intent_code", "media_type", "size_bytes", "status", "scan_version",
    "scan_outcome", "revision", "created_at", "updated_at",
  ]);
  if (!/^att_[0-9a-f]{32}$/u.test(value.attachment_id)
    || !ATTACHMENT_INTENTS.has(value.intent_code)
    || !new Set(["text/plain", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]).has(value.media_type)
    || !Number.isSafeInteger(value.size_bytes) || value.size_bytes < 1 || value.size_bytes > 52_428_800
    || !new Set(["quarantined", "available", "rejected", "scan_timeout"]).has(value.status)
    || value.scan_version !== "ticket-attachment-scan-v1"
    || (value.scan_outcome !== null && !new Set(["clean", "malicious", "structure_invalid", "owner_changed", "timeout", "unavailable"]).has(value.scan_outcome))) throw new TypeError();
  positive(value.revision);
  timestamp(value.created_at);
  timestamp(value.updated_at);
}

function validateSla(value) {
  exact(value, [
    "state", "generation", "first_response_deadline_at", "resolution_deadline_at", "risk_at",
    "first_responded_at", "risk_generation", "escalation_generation", "revision",
  ]);
  if (!new Set(["running", "paused_customer", "paused_policy", "stopped"]).has(value.state)) throw new TypeError();
  positive(value.generation);
  timestamp(value.first_response_deadline_at);
  timestamp(value.resolution_deadline_at);
  timestamp(value.risk_at);
  nullableTimestamp(value.first_responded_at);
  nonNegative(value.risk_generation);
  nonNegative(value.escalation_generation);
  positive(value.revision);
}

function validateEscalation(value) {
  exact(value, ["escalation_id", "source_code", "reason_code", "generation", "status", "created_at"]);
  if (!/^esc_[0-9a-f]{32}$/u.test(value.escalation_id)
    || !new Set(["customer_requested", "sla_at_risk", "sla_breached", "platform_manual"]).has(value.source_code)
    || typeof value.reason_code !== "string" || !REASON.test(value.reason_code)
    || !new Set(["open", "acknowledged", "closed"]).has(value.status)) throw new TypeError();
  positive(value.generation);
  timestamp(value.created_at);
}

function validateSurvey(value) {
  exact(value, ["rating", "comment", "created_at"]);
  if (!Number.isSafeInteger(value.rating) || value.rating < 1 || value.rating > 5
    || (value.comment !== null && (typeof value.comment !== "string" || value.comment.length < 1 || value.comment.length > 2000))) throw new TypeError();
  timestamp(value.created_at);
}

function patchPayload(changes, actorDomain) {
  if (!changes || Object.getPrototypeOf(changes) !== Object.prototype) invalidRequest();
  const allowed = new Set([
    "status", "message", "message_visibility", "assignee_operator_id", "sla_action",
    "survey_rating", "survey_comment", "reason_code",
  ]);
  if (!Object.keys(changes).length || Object.keys(changes).some((key) => !allowed.has(key))) invalidRequest();
  const body = {};
  if (changes.status !== undefined) body.status = choice(changes.status, STATUSES);
  if (changes.message !== undefined) body.message = boundedText(changes.message, 1, 8000);
  if (changes.message_visibility !== undefined) body.message_visibility = choice(changes.message_visibility, VISIBILITIES);
  if (changes.assignee_operator_id !== undefined) {
    if (!OPERATOR_ID.test(String(changes.assignee_operator_id))) invalidRequest();
    body.assignee_operator_id = String(changes.assignee_operator_id);
  }
  if (changes.sla_action !== undefined) body.sla_action = choice(changes.sla_action, new Set(["pause_policy", "resume"]));
  if (changes.survey_rating !== undefined) body.survey_rating = integer(changes.survey_rating, 1, 5);
  if (changes.survey_comment !== undefined) body.survey_comment = boundedText(changes.survey_comment, 1, 2000);
  if (changes.reason_code !== undefined) body.reason_code = reason(changes.reason_code);
  if (body.message && body.message_visibility === undefined) body.message_visibility = "customer";
  if (actorDomain === "workspace" && (body.message_visibility === "internal"
    || body.assignee_operator_id !== undefined || body.sla_action !== undefined)) invalidRequest();
  return body;
}

function apiUrl(base, path) {
  let url;
  try { url = new URL(path, String(base).replace(/\/+$/u, "") + "/"); } catch { invalidRequest(); }
  if (!new Set(["http:", "https:"]).has(url.protocol) || url.username || url.password) invalidRequest();
  return url.toString();
}

function readCookie(name, cookieText = globalThis.document?.cookie ?? "") {
  const prefix = `${encodeURIComponent(name)}=`;
  for (const part of String(cookieText).split(";")) {
    const value = part.trim();
    if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
  }
  return "";
}

function exact(value, keys) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype
    || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) throw new TypeError();
}
function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}
function requireDomain(value) { if (!DOMAINS.has(value)) invalidRequest(); return value; }
function ticket(value) { const text = String(value ?? ""); if (!TICKET_ID.test(text)) invalidRequest(); return text; }
function choice(value, choices) { const text = String(value ?? ""); if (!choices.has(text)) invalidRequest(); return text; }
function boundedText(value, min, max) { const text = String(value ?? "").trim(); if (text.length < min || text.length > max) invalidRequest(); return text; }
function optionalBoundedText(value, min, max) { return value === null || value === undefined || value === "" ? null : boundedText(value, min, max); }
function integer(value, min, max) { const number = Number(value); if (!Number.isSafeInteger(number) || number < min || number > max) invalidRequest(); return number; }
function positive(value) { if (!Number.isSafeInteger(value) || value < 1) throw new TypeError(); }
function nonNegative(value) { if (!Number.isSafeInteger(value) || value < 0) throw new TypeError(); }
function timestamp(value) { if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) throw new TypeError(); }
function nullableTimestamp(value) { if (value !== null) timestamp(value); }
function reason(value) { const text = String(value ?? ""); if (!REASON.test(text)) invalidRequest(); return text; }
function requireIdempotency(value) { const text = String(value ?? ""); if (!IDEMPOTENCY.test(text)) invalidRequest(); return text; }
function chain(value) { if (!SAFE_CHAIN.test(String(value ?? ""))) throw new TypeError(); }
function safeChain(value) { const text = String(value ?? ""); return SAFE_CHAIN.test(text) ? text : undefined; }
function safeCode(value) { const text = String(value ?? ""); return /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(text) ? text : "support.unavailable"; }
async function readJson(response) { try { return await response.json(); } catch { return {}; } }
function invalidRequest(code = "support.request_invalid") {
  throw new AuthApiError({ status: 0, code, message: "支持请求参数无效。" });
}
