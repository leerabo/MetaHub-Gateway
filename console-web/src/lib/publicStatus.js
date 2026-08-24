const STATUS = new Set([
  "operational", "degraded_performance", "partial_outage", "major_outage", "maintenance",
]);
const INCIDENT_STATUS = new Set([
  "published", "monitoring", "resolved", "postmortem_published",
]);

export class PublicStatusError extends Error {
  constructor(code = "status.unavailable", status = 0) {
    super("Current service status is temporarily unavailable.");
    this.name = "PublicStatusError";
    this.code = code;
    this.status = status;
  }
}

export async function getPublicStatus({ statusApiUrl, signal } = {}) {
  const response = await fetch(apiUrl(statusApiUrl, "/public/status"), {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
    signal,
  });
  const value = await readJson(response);
  if (!response.ok) throw new PublicStatusError(value.code, response.status);
  if (!validStatus(value)) throw new PublicStatusError("status.response_invalid", 502);
  return value;
}

export async function mutatePublicStatusSubscription({
  controlApiUrl,
  operation,
  createIntent,
  contact,
  scope,
  token,
  signal,
} = {}) {
  const payload = operation === "create"
    ? { operation, create_intent: createIntent, channel: "email", contact, scope }
    : { operation, token };
  const response = await fetch(apiUrl(controlApiUrl, "/status/subscriptions"), {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(payload),
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
    signal,
  });
  const value = await readJson(response);
  if (!response.ok || !plain(value) || !exact(value, ["accepted", "request_id", "trace_id"])
    || value.accepted !== true) {
    throw new PublicStatusError(value.code, response.status);
  }
  return value;
}

function validStatus(value) {
  return plain(value) && exact(value, [
    "wire_schema_version", "generation", "updated_at", "expires_at", "overall_status",
    "components", "incidents", "maintenance_windows",
  ]) && value.wire_schema_version === 1 && integer(value.generation, 1)
    && timestamp(value.updated_at) && timestamp(value.expires_at)
    && Date.parse(value.expires_at) > Date.parse(value.updated_at)
    && STATUS.has(value.overall_status)
    && bounded(value.components, 1, 100, validComponent)
    && bounded(value.incidents, 0, 500, validIncident)
    && bounded(value.maintenance_windows, 0, 500, validMaintenance)
    && !containsPrivateKey(value);
}

function validComponent(value) {
  return plain(value) && exact(value, ["code", "name", "group", "status", "revision"])
    && code(value.code) && text(value.name, 2, 80)
    && new Set(["gateway", "control", "identity", "billing", "integrations"]).has(value.group)
    && STATUS.has(value.status) && integer(value.revision, 1);
}

function validIncident(value) {
  const keys = [
    "reference", "title", "severity", "status", "acknowledged_at", "updated_at", "updates",
  ];
  const withPostmortem = Object.hasOwn(value ?? {}, "postmortem") ? [...keys, "postmortem"] : keys;
  return plain(value) && exact(value, withPostmortem)
    && text(value.reference, 8, 32) && text(value.title, 5, 160)
    && new Set(["minor", "major", "critical"]).has(value.severity)
    && INCIDENT_STATUS.has(value.status) && timestamp(value.acknowledged_at)
    && timestamp(value.updated_at) && bounded(value.updates, 1, 500, validUpdate)
    && (!Object.hasOwn(value, "postmortem") || validPostmortem(value.postmortem));
}

function validUpdate(value) {
  return plain(value) && exact(value, [
    "status", "message", "affected_components", "affected_regions", "published_at",
  ]) && new Set(["published", "monitoring", "resolved"]).has(value.status)
    && text(value.message, 10, 2000)
    && bounded(value.affected_components, 0, 50, code)
    && bounded(value.affected_regions, 0, 50, code) && timestamp(value.published_at);
}

function validPostmortem(value) {
  return plain(value) && exact(value, [
    "summary", "timeline", "cause_category", "corrective_actions", "published_at",
  ]) && text(value.summary, 20, 4000) && bounded(value.timeline, 1, 100, (item) => text(item, 1, 500))
    && new Set([
      "capacity", "dependency", "deployment", "configuration", "software", "network", "other",
    ]).has(value.cause_category)
    && bounded(value.corrective_actions, 1, 100, (item) => text(item, 1, 500))
    && timestamp(value.published_at);
}

function validMaintenance(value) {
  return plain(value) && exact(value, [
    "reference", "title", "message", "status", "affected_components", "affected_regions",
    "starts_at", "ends_at",
  ]) && text(value.reference, 8, 32) && text(value.title, 5, 160)
    && text(value.message, 10, 2000)
    && new Set(["planned", "in_progress"]).has(value.status)
    && bounded(value.affected_components, 0, 50, code)
    && bounded(value.affected_regions, 0, 50, code)
    && timestamp(value.starts_at) && timestamp(value.ends_at)
    && Date.parse(value.ends_at) > Date.parse(value.starts_at);
}

function containsPrivateKey(value) {
  const forbidden = new Set([
    "account_id", "authorization", "contact", "email", "endpoint", "internal", "password",
    "prompt", "provider_secret", "response", "secret", "tenant_id", "token", "workspace_id",
  ]);
  if (Array.isArray(value)) return value.some(containsPrivateKey);
  if (!plain(value)) return false;
  return Object.entries(value).some(([key, item]) => forbidden.has(key.toLowerCase())
    || containsPrivateKey(item));
}

function apiUrl(baseUrl, path) {
  let base;
  try { base = new URL(String(baseUrl)); } catch { throw new PublicStatusError("status.request_invalid"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw new PublicStatusError("status.request_invalid");
  }
  return new URL(path, base).toString();
}
function plain(value) { return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype; }
function exact(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}
function bounded(value, minimum, maximum, validator) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum
    && value.every(validator);
}
function integer(value, minimum) { return Number.isSafeInteger(value) && value >= minimum; }
function text(value, minimum, maximum) {
  return typeof value === "string" && value.length >= minimum && value.length <= maximum
    && !/[<>\u0000-\u001f\u007f]/u.test(value);
}
function code(value) { return typeof value === "string" && /^[a-z0-9][a-z0-9-]{1,47}$/u.test(value); }
function timestamp(value) { return typeof value === "string" && Number.isFinite(Date.parse(value)); }
async function readJson(response) {
  try {
    const value = await response.json();
    return plain(value) ? value : {};
  } catch {
    return {};
  }
}
