import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const INCIDENT_ID = /^inc_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LINK = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const CODE = /^[a-z0-9][a-z0-9-]{1,47}$/u;
const COMPONENT_STATUS = new Set([
  "operational", "degraded_performance", "partial_outage", "major_outage", "maintenance",
]);
const INCIDENT_STATUS = new Set([
  "draft", "pending_security_review", "published", "monitoring", "resolved",
  "postmortem_published",
]);

export function getPlatformStatusIncidents({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/status-incidents", { signal }, validStatusResponse);
}

export function createPlatformIncident(input = {}) {
  const payload = {
    owner_workspace_id: String(input.ownerWorkspaceId ?? ""),
    title: String(input.title ?? ""),
    classification: String(input.classification ?? ""),
    visibility: String(input.visibility ?? ""),
    severity: String(input.severity ?? ""),
    acknowledged_at: String(input.acknowledgedAt ?? ""),
    initial_update: input.initialUpdate == null ? null : String(input.initialUpdate),
    affected_components: codes(input.affectedComponents),
    affected_regions: codes(input.affectedRegions),
  };
  if (!WORKSPACE_ID.test(payload.owner_workspace_id) || !text(payload.title, 5, 160)
    || !new Set(["reliability", "security"]).has(payload.classification)
    || !new Set(["public", "workspace"]).has(payload.visibility)
    || !new Set(["minor", "major", "critical"]).has(payload.severity)
    || !timestamp(payload.acknowledged_at)
    || ((payload.classification === "reliability") !== text(payload.initial_update, 10, 2000))) {
    invalid();
  }
  return write(input.controlApiUrl, "/platform/incidents", payload, input, validIncidentResponse);
}

export function approvePlatformSecurityIncident(input = {}) {
  return incidentWrite("security-approval", input, {
    revision: Number(input.revision),
    public_message: String(input.publicMessage ?? ""),
    affected_components: codes(input.affectedComponents),
    affected_regions: codes(input.affectedRegions),
  });
}

export function updatePlatformIncident(input = {}) {
  return incidentWrite("updates", input, {
    revision: Number(input.revision),
    status: String(input.status ?? ""),
    public_message: String(input.publicMessage ?? ""),
    affected_components: codes(input.affectedComponents),
    affected_regions: codes(input.affectedRegions),
  });
}

export function publishPlatformPostmortem(input = {}) {
  const payload = {
    revision: Number(input.revision),
    summary: String(input.summary ?? ""),
    timeline: strings(input.timeline),
    cause_category: String(input.causeCategory ?? ""),
    corrective_actions: strings(input.correctiveActions),
  };
  if (!integer(payload.revision, 1) || !text(payload.summary, 20, 4000)
    || !bounded(payload.timeline, 1, 100, (item) => text(item, 1, 500))
    || !new Set([
      "capacity", "dependency", "deployment", "configuration", "software", "network", "other",
    ]).has(payload.cause_category)
    || !bounded(payload.corrective_actions, 1, 100, (item) => text(item, 1, 500))) invalid();
  return incidentWrite("postmortem", input, payload);
}

export function createPlatformMaintenanceWindow(input = {}) {
  const payload = {
    owner_workspace_id: String(input.ownerWorkspaceId ?? ""),
    title: String(input.title ?? ""),
    public_message: String(input.publicMessage ?? ""),
    visibility: String(input.visibility ?? ""),
    affected_components: codes(input.affectedComponents),
    affected_regions: codes(input.affectedRegions),
    starts_at: String(input.startsAt ?? ""),
    ends_at: String(input.endsAt ?? ""),
  };
  if (!WORKSPACE_ID.test(payload.owner_workspace_id) || !text(payload.title, 5, 160)
    || !text(payload.public_message, 10, 2000)
    || !new Set(["public", "workspace"]).has(payload.visibility)
    || !timestamp(payload.starts_at) || !timestamp(payload.ends_at)
    || Date.parse(payload.ends_at) <= Date.parse(payload.starts_at)) invalid();
  return write(
    input.controlApiUrl,
    "/platform/maintenance-windows",
    payload,
    input,
    validMaintenanceResponse,
  );
}

function incidentWrite(action, input, payload) {
  const id = String(input.incidentId ?? "");
  if (!INCIDENT_ID.test(id) || !integer(payload.revision, 1)
    || (Object.hasOwn(payload, "public_message") && !text(payload.public_message, 10, 2000))
    || (Object.hasOwn(payload, "status") && !new Set(["monitoring", "resolved"]).has(payload.status))) {
    invalid();
  }
  return write(
    input.controlApiUrl,
    `/platform/incidents/${encodeURIComponent(id)}/${action}`,
    payload,
    input,
    validIncidentResponse,
    payload.revision,
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
    throw clientError("status.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = typeof payload.code === "string" ? payload.code : "status.unavailable";
    throw clientError(code, response.status, LINK.test(String(payload.request_id ?? ""))
      ? payload.request_id : "-");
  }
  if (!validator(payload)) throw clientError("status.response_invalid", 502);
  return payload;
}

function validStatusResponse(value) {
  return plain(value) && exact(value, [
    "components", "incidents", "maintenance_windows", "bundle_generation", "request_id", "trace_id",
  ]) && bounded(value.components, 1, 100, validComponent)
    && bounded(value.incidents, 0, 500, validIncident)
    && bounded(value.maintenance_windows, 0, 500, validMaintenance)
    && (value.bundle_generation === null || integer(value.bundle_generation, 1)) && links(value)
    && !privateKey(value);
}
function validComponent(value) {
  return plain(value) && exact(value, ["code", "name", "group", "status", "revision"])
    && CODE.test(value.code) && text(value.name, 2, 80)
    && new Set(["gateway", "control", "identity", "billing", "integrations"]).has(value.group)
    && COMPONENT_STATUS.has(value.status) && integer(value.revision, 1);
}
function validIncident(value) {
  return plain(value) && exact(value, [
    "incident_id", "reference", "title", "classification", "visibility", "severity", "status",
    "updates", "postmortem", "security_approval_available", "revision", "acknowledged_at", "updated_at",
  ]) && INCIDENT_ID.test(value.incident_id) && text(value.reference, 8, 32)
    && text(value.title, 5, 160) && new Set(["reliability", "security"]).has(value.classification)
    && new Set(["public", "workspace"]).has(value.visibility)
    && new Set(["minor", "major", "critical"]).has(value.severity)
    && INCIDENT_STATUS.has(value.status) && bounded(value.updates, 0, 500, validUpdate)
    && (value.postmortem === null || validPostmortem(value.postmortem))
    && typeof value.security_approval_available === "boolean"
    && integer(value.revision, 1) && timestamp(value.acknowledged_at) && timestamp(value.updated_at);
}
function validUpdate(value) {
  return plain(value) && exact(value, [
    "incident_update_id", "status", "message", "affected_components", "affected_regions", "published_at",
  ]) && /^iup_[0-9a-f]{32}$/u.test(value.incident_update_id)
    && new Set(["published", "monitoring", "resolved"]).has(value.status)
    && text(value.message, 10, 2000) && bounded(value.affected_components, 0, 50, code)
    && bounded(value.affected_regions, 0, 50, code) && timestamp(value.published_at);
}
function validPostmortem(value) {
  return plain(value) && exact(value, [
    "summary", "timeline", "cause_category", "corrective_actions", "published_at",
  ]) && text(value.summary, 20, 4000) && Array.isArray(value.timeline)
    && Array.isArray(value.corrective_actions) && timestamp(value.published_at);
}
function validMaintenance(value) {
  return plain(value) && exact(value, [
    "maintenance_window_id", "reference", "title", "message", "visibility", "status",
    "affected_components", "affected_regions", "starts_at", "ends_at", "revision",
  ]) && /^mtn_[0-9a-f]{32}$/u.test(value.maintenance_window_id)
    && text(value.reference, 8, 32) && text(value.title, 5, 160) && text(value.message, 10, 2000)
    && new Set(["public", "workspace"]).has(value.visibility)
    && new Set(["planned", "in_progress", "completed", "cancelled"]).has(value.status)
    && bounded(value.affected_components, 0, 50, code)
    && bounded(value.affected_regions, 0, 50, code)
    && timestamp(value.starts_at) && timestamp(value.ends_at) && integer(value.revision, 1);
}
function validIncidentResponse(value) {
  return plain(value) && exact(value, ["incident", "idempotent_replay", "request_id", "trace_id"])
    && validIncident(value.incident) && typeof value.idempotent_replay === "boolean" && links(value);
}
function validMaintenanceResponse(value) {
  return plain(value) && exact(value, [
    "maintenance_window", "idempotent_replay", "request_id", "trace_id",
  ]) && validMaintenance(value.maintenance_window)
    && typeof value.idempotent_replay === "boolean" && links(value);
}
function privateKey(value) {
  const blocked = new Set(["tenant_id", "contact", "email", "token", "internal_endpoint"]);
  if (Array.isArray(value)) return value.some(privateKey);
  if (!plain(value)) return false;
  return Object.entries(value).some(([key, item]) => blocked.has(key) || privateKey(item));
}
function codes(value) { return strings(value).map((item) => item.toLowerCase()); }
function strings(value) {
  if (Array.isArray(value)) return value.map(String);
  return String(value ?? "").split(",").map((item) => item.trim()).filter(Boolean);
}
function code(value) { return typeof value === "string" && CODE.test(value); }
function text(value, minimum, maximum) {
  return typeof value === "string" && value.length >= minimum && value.length <= maximum
    && !/[<>\u0000-\u001f\u007f]/u.test(value);
}
function timestamp(value) { return typeof value === "string" && Number.isFinite(Date.parse(value)); }
function integer(value, minimum) { return Number.isSafeInteger(value) && value >= minimum; }
function bounded(value, minimum, maximum, validator) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum && value.every(validator);
}
function plain(value) { return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype; }
function exact(value, keys) {
  const actual = Object.keys(value).sort(); const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}
function links(value) { return LINK.test(value.request_id) && LINK.test(value.trace_id); }
function apiUrl(controlApiUrl, path) {
  let base; try { base = new URL(String(controlApiUrl)); } catch { invalid(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) invalid();
  return new URL(path, base).toString();
}
function invalid() { throw clientError("status.request_invalid", 0); }
function clientError(code, status, requestId = "-") {
  return new PlatformApiError({ code, status, requestId, message: "Status communication request failed." });
}
async function readJson(response) { try { const value = await response.json(); return plain(value) ? value : {}; } catch { return {}; } }
