const PLATFORM_CSRF_COOKIE = "__Host-metahub-platform-csrf";
const HEX64 = /^[0-9a-f]{64}$/u;
const CODE = /^[a-z][a-z0-9_.-]{0,79}$/u;
const PRIVATE_KEYS = new Set([
  "account_id", "authorization", "cookie", "csrf", "password", "prompt", "response",
  "secret", "tenant_id", "token", "workspace_id",
]);

export class PlatformConsoleExperienceError extends Error {
  constructor({ status = 0, code = "platform.console.request_failed", message, requestId = "-", traceId = "-" } = {}) {
    super(safeText(message) || "平台控制台请求未完成。");
    this.name = "PlatformConsoleExperienceError";
    this.status = Number(status) || 0;
    this.code = safeCode(code, "platform.console.request_failed");
    this.requestId = safeChain(requestId);
    this.traceId = safeChain(traceId);
  }
}

export function getPlatformConsoleOverview({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/overview", { signal, normalize: normalizePlatformOverview });
}

export function queryPlatformConsoleAssistant({ controlApiUrl, question, signal } = {}) {
  const normalized = String(question ?? "");
  if (!normalized.trim() || [...normalized].length > 2000 || normalized.includes("\0")) invalidRequest();
  return request(controlApiUrl, "/platform/assistant/query", {
    method: "POST",
    body: { question: normalized },
    csrf: true,
    signal,
    normalize: normalizePlatformAssistant,
  });
}

export function listPlatformConsoleApprovals({
  controlApiUrl,
  status = "",
  risk = "",
  sourceDomain = "",
  cursor: nextCursor = null,
  limit = 50,
  signal,
} = {}) {
  const query = approvalQuery({ status, risk, sourceDomain, cursor: nextCursor, limit });
  return request(controlApiUrl, `/platform/approvals?${query}`, {
    signal,
    normalize: normalizePlatformApprovals,
  });
}

export function normalizePlatformOverview(value) {
  exact(value, [
    "domain", "generation", "navigation_epoch", "observed_at", "permission_fingerprint",
    "permission_revision", "request_id", "sections", "status", "trace_id",
  ]);
  if (value.domain !== "platform" || !new Set(["ready", "partial", "stale"]).has(value.status)) invalidResponse();
  return deepFreeze({
    ...value,
    generation: integer(value.generation, 1),
    navigation_epoch: integer(value.navigation_epoch, 1),
    observed_at: timestamp(value.observed_at),
    sections: array(value.sections, 16).map(consoleSection),
    permission_revision: integer(value.permission_revision, 1),
    permission_fingerprint: fingerprint(value.permission_fingerprint),
    request_id: chain(value.request_id),
    trace_id: chain(value.trace_id),
  });
}

export function normalizePlatformAssistant(value) {
  exact(value, [
    "answer_code", "citations", "claims", "domain", "navigation_epoch", "outcome",
    "permission_fingerprint", "permission_revision", "query_id", "query_type", "request_id",
    "sources", "trace_id",
  ]);
  if (value.domain !== "platform"
    || !new Set(["operations_summary", "security_summary", "billing_summary", "provider_summary", "unknown"]).has(value.query_type)
    || !new Set(["answered", "partial", "unsupported"]).has(value.outcome)) invalidResponse();
  return deepFreeze({
    ...value,
    query_id: boundedText(value.query_id, 1, 160),
    answer_code: code(value.answer_code),
    claims: array(value.claims, 100).map(assistantClaim),
    citations: array(value.citations, 50).map(assistantCitation),
    sources: array(value.sources, 50).map(assistantSource),
    navigation_epoch: integer(value.navigation_epoch, 1),
    permission_revision: integer(value.permission_revision, 1),
    permission_fingerprint: fingerprint(value.permission_fingerprint),
    request_id: chain(value.request_id),
    trace_id: chain(value.trace_id),
  });
}

export function normalizePlatformApprovals(value) {
  exact(value, [
    "domain", "items", "navigation_epoch", "next_cursor", "permission_fingerprint",
    "permission_revision", "request_id", "trace_id",
  ]);
  if (value.domain !== "platform") invalidResponse();
  return deepFreeze({
    ...value,
    items: array(value.items, 100).map(approvalItem),
    next_cursor: cursor(value.next_cursor),
    navigation_epoch: integer(value.navigation_epoch, 1),
    permission_revision: integer(value.permission_revision, 1),
    permission_fingerprint: fingerprint(value.permission_fingerprint),
    request_id: chain(value.request_id),
    trace_id: chain(value.trace_id),
  });
}

async function request(controlApiUrl, path, options) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = readExactCookie(PLATFORM_CSRF_COOKIE);
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new PlatformConsoleExperienceError({
      code: "platform.console.network_unavailable",
      message: "暂时无法连接平台控制台服务。",
    });
  }
  const payload = await json(response);
  if (!response.ok) throw apiError(response.status, payload);
  const normalized = options.normalize(payload);
  requirePrivateResponse(response, normalized.permission_fingerprint);
  return normalized;
}

function approvalQuery({ status, risk, sourceDomain, cursor: nextCursor, limit }) {
  const parameters = new URLSearchParams();
  if (status) {
    if (!new Set(["pending", "approved", "rejected", "expired"]).has(status)) invalidRequest();
    parameters.set("status", status);
  }
  if (risk) {
    if (!new Set(["standard", "high", "critical"]).has(risk)) invalidRequest();
    parameters.set("risk", risk);
  }
  if (sourceDomain) parameters.set("source_domain", code(sourceDomain));
  if (nextCursor !== null && nextCursor !== undefined) parameters.set("cursor", cursor(nextCursor));
  parameters.set("limit", String(integer(limit, 1, 100)));
  return parameters.toString();
}

function consoleSection(value) {
  exact(value, ["freshness", "metrics", "source_code", "source_revision", "source_watermark"]);
  if (!new Set(["fresh", "stale", "unavailable"]).has(value.freshness)) invalidResponse();
  return {
    ...value,
    source_code: code(value.source_code),
    source_revision: integer(value.source_revision, 0),
    source_watermark: boundedText(value.source_watermark, 1, 160),
    metrics: array(value.metrics, 40).map(consoleMetric),
  };
}

function consoleMetric(value) {
  exact(value, ["action_code", "metric_code", "reason_code", "state_code", "value"]);
  return {
    ...value,
    metric_code: code(value.metric_code),
    value: value.value === null ? null : integer(value.value, 0),
    state_code: code(value.state_code),
    action_code: optionalCode(value.action_code),
    reason_code: optionalCode(value.reason_code),
  };
}

function assistantClaim(value) {
  exact(value, ["claim_code", "state_code", "value"]);
  return { claim_code: code(value.claim_code), value: value.value === null ? null : integer(value.value, 0), state_code: code(value.state_code) };
}

function assistantCitation(value) {
  exact(value, ["label_code", "navigation_id", "source_type"]);
  return { source_type: code(value.source_type), label_code: code(value.label_code), navigation_id: code(value.navigation_id) };
}

function assistantSource(value) {
  exact(value, ["source_type", "status"]);
  if (!new Set(["ready", "stale", "unavailable"]).has(value.status)) invalidResponse();
  return { source_type: code(value.source_type), status: value.status };
}

function approvalItem(value) {
  exact(value, [
    "can_open", "deadline_at", "decision_contract_code", "projection_id", "risk_band",
    "source_domain", "source_revision", "source_type", "status", "summary_code",
  ]);
  if (!new Set(["pending", "approved", "rejected", "expired"]).has(value.status)
    || !new Set(["standard", "high", "critical"]).has(value.risk_band)
    || typeof value.can_open !== "boolean") invalidResponse();
  return {
    ...value,
    projection_id: boundedText(value.projection_id, 1, 160),
    source_domain: code(value.source_domain),
    source_type: code(value.source_type),
    source_revision: integer(value.source_revision, 1),
    summary_code: code(value.summary_code),
    decision_contract_code: code(value.decision_contract_code),
    deadline_at: timestamp(value.deadline_at),
  };
}

function requirePrivateResponse(response, expectedFingerprint) {
  const cacheControl = response.headers?.get?.("cache-control")?.toLowerCase() ?? "";
  const pragma = response.headers?.get?.("pragma")?.toLowerCase() ?? "";
  const vary = response.headers?.get?.("vary")?.toLowerCase() ?? "";
  const headerFingerprint = response.headers?.get?.("x-permission-fingerprint") ?? "";
  if (!cacheControl.includes("no-store") || !cacheControl.includes("private")
    || pragma !== "no-cache" || !vary.split(",").map((item) => item.trim()).includes("cookie")
    || headerFingerprint !== expectedFingerprint) invalidResponse();
}

function apiUrl(controlApiUrl, path) {
  try {
    const base = new URL(String(controlApiUrl));
    if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) throw new Error();
    return new URL(path, base).toString();
  } catch {
    throw new PlatformConsoleExperienceError({ code: "platform.console.configuration_invalid", message: "平台服务地址无效。" });
  }
}

function readExactCookie(name, cookieText = globalThis.document?.cookie ?? "") {
  const values = String(cookieText).split(";").map((item) => item.trim())
    .filter((item) => item.startsWith(`${name}=`)).map((item) => item.slice(name.length + 1));
  if (values.length !== 1) invalidRequest("platform.console.csrf_invalid");
  const value = decodeURIComponent(values[0]);
  if (!/^[A-Za-z0-9_-]{43,128}$/u.test(value)) invalidRequest("platform.console.csrf_invalid");
  return value;
}

function exact(value, keys) {
  if (!plain(value) || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) invalidResponse();
  for (const key of Object.keys(value)) if (PRIVATE_KEYS.has(key.toLowerCase())) invalidResponse();
}
function array(value, maximum) { if (!Array.isArray(value) || value.length > maximum) invalidResponse(); return value; }
function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER) { if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalidResponse(); return value; }
function code(value) { if (typeof value !== "string" || !CODE.test(value)) invalidResponse(); return value; }
function optionalCode(value) { return value === null ? null : code(value); }
function fingerprint(value) { if (typeof value !== "string" || !HEX64.test(value)) invalidResponse(); return value; }
function cursor(value) { if (value === null) return null; return boundedText(value, 43, 512); }
function timestamp(value) { const text = boundedText(value, 20, 40); if (!Number.isFinite(Date.parse(text))) invalidResponse(); return text; }
function chain(value) { const text = boundedText(value, 1, 160); if (!/^[A-Za-z0-9_.:-]+$/u.test(text)) invalidResponse(); return text; }
function boundedText(value, minimum, maximum) { if (typeof value !== "string" || value.length < minimum || value.length > maximum || /[\u0000-\u001f\u007f-\u009f]/u.test(value)) invalidResponse(); return value; }
function plain(value) { return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype; }
function safeText(value) { return typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f-\u009f]/gu, " ").slice(0, 240) : ""; }
function safeCode(value, fallback) { const text = String(value ?? ""); return /^[a-z][a-z0-9_.]{1,120}$/u.test(text) ? text : fallback; }
function safeChain(value) { const text = String(value ?? ""); return /^[A-Za-z0-9_.:-]{1,160}$/u.test(text) ? text : "-"; }
function deepFreeze(value) { if (value && typeof value === "object" && !Object.isFrozen(value)) { Object.freeze(value); for (const child of Object.values(value)) deepFreeze(child); } return value; }
function invalidRequest(codeValue = "platform.console.request_invalid") { throw new PlatformConsoleExperienceError({ code: codeValue, message: "平台控制台请求字段无效。" }); }
function invalidResponse() { throw new PlatformConsoleExperienceError({ status: 502, code: "platform.console.response_invalid", message: "平台控制台响应格式无效。" }); }
function apiError(status, payload) { return new PlatformConsoleExperienceError({ status, code: payload?.code, message: payload?.message, requestId: payload?.request_id, traceId: payload?.trace_id }); }
async function json(response) { try { const value = await response.json(); return plain(value) ? value : {}; } catch { return {}; } }
