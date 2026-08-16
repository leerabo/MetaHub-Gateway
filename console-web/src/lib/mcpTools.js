import { AuthApiError } from "./auth.js";

const SERVER_ID = /^mcps_[0-9a-f]{32}$/;
const TOOL_ID = /^mcpt_[0-9a-f]{32}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const SERVER_STATUSES = new Set(["pending_sync", "active", "sync_failed", "disabled"]);
const TOOL_STATUSES = new Set(["review_required", "active", "disabled"]);
const INVOCATION_STATUSES = new Set([
  "approval_pending", "queued", "probing", "calling", "succeeded", "failed",
  "blocked", "rejected", "outcome_unknown",
]);
const RISK_LEVELS = new Set(["standard", "high"]);
let idempotencySequence = 0;

/** Read tenant MCP Server summaries. Endpoint URLs and credentials are never accepted as filters. */
export async function listMcpServers({
  controlApiUrl,
  token,
  status = "",
  cursor = null,
  limit = 50,
  signal,
}) {
  const query = boundedListQuery({ status, cursor, limit }, SERVER_STATUSES);
  return mcpRequest(`${controlApiUrl}/mcp/servers${query}`, { token, signal });
}

/** Persist a Server registration intent. The caller must erase endpoint/token inputs before awaiting. */
export async function registerMcpServer({
  controlApiUrl,
  token,
  name,
  endpointUrl,
  authType = "none",
  bearerToken = "",
  idempotencyKey,
  signal,
}) {
  const request = normalizeServerRegistration({ name, endpointUrl, authType, bearerToken });
  return mcpRequest(`${controlApiUrl}/mcp/servers`, {
    token,
    method: "POST",
    idempotencyKey: validIdempotencyKey(idempotencyKey),
    body: request,
    signal,
  });
}

/** Enqueue a recoverable tools/list refresh without exposing the Server endpoint to the browser. */
export async function refreshMcpServerTools({
  controlApiUrl,
  token,
  serverId,
  expectedRevision,
  idempotencyKey,
  signal,
}) {
  const normalizedServerId = requireId(serverId, SERVER_ID);
  const revision = positiveInteger(expectedRevision);
  return mcpRequest(
    `${controlApiUrl}/mcp/servers/${encodeURIComponent(normalizedServerId)}/refresh-tools`,
    {
      token,
      method: "POST",
      idempotencyKey: validIdempotencyKey(idempotencyKey),
      body: { expected_revision: revision },
      signal,
    },
  );
}

/** Enable or disable one Server with a backend-owned revision and an exact closed body. */
export async function updateMcpServerStatus({
  controlApiUrl,
  token,
  serverId,
  expectedRevision,
  status,
  idempotencyKey,
  signal,
}) {
  const normalizedServerId = requireId(serverId, SERVER_ID);
  const revision = positiveInteger(expectedRevision);
  const nextStatus = normalizedEnum(status, new Set(["active", "disabled"]));
  return mcpRequest(`${controlApiUrl}/mcp/servers/${encodeURIComponent(normalizedServerId)}`, {
    token,
    method: "PATCH",
    idempotencyKey: validIdempotencyKey(idempotencyKey),
    body: { expected_revision: revision, status: nextStatus },
    signal,
  });
}

/** Read safe Tool list summaries. Canonical Schema and description only come from getMcpTool. */
export async function listMcpTools({
  controlApiUrl,
  token,
  serverId = "",
  status = "",
  riskLevel = "",
  approvalRequired = "",
  cursor = null,
  limit = 50,
  signal,
}) {
  const values = { status, cursor, limit };
  if (serverId) values.server_id = requireId(serverId, SERVER_ID);
  if (riskLevel) {
    if (!RISK_LEVELS.has(riskLevel)) throw localError("mcp.query_invalid", 422);
    values.risk = riskLevel;
  }
  if (approvalRequired !== "") {
    if (typeof approvalRequired !== "boolean") throw localError("mcp.query_invalid", 422);
    values.approval_required = String(approvalRequired);
  }
  const query = boundedListQuery(values, TOOL_STATUSES);
  return mcpRequest(`${controlApiUrl}/mcp/tools${query}`, { token, signal });
}

/** Load one bounded Tool detail, including original/effective Schema, only after explicit disclosure. */
export async function getMcpTool({ controlApiUrl, token, toolId, signal }) {
  const normalizedToolId = requireId(toolId, TOOL_ID);
  return mcpRequest(`${controlApiUrl}/mcp/tools/${encodeURIComponent(normalizedToolId)}`, {
    token,
    signal,
  });
}

/** Apply the exact governance policy contract; Schema/name/server ownership are never writable here. */
export async function updateMcpToolPolicy({ controlApiUrl, token, toolId, policy, signal }) {
  const normalizedToolId = requireId(toolId, TOOL_ID);
  return mcpRequest(`${controlApiUrl}/mcp/tools/${encodeURIComponent(normalizedToolId)}`, {
    token,
    method: "PATCH",
    body: normalizeMcpToolPolicy(policy),
    signal,
  });
}

/** Read invocation safety summaries. Arguments, results, URLs and private fingerprints are not queryable. */
export async function listMcpInvocations({
  controlApiUrl,
  token,
  status = "",
  serverId = "",
  toolId = "",
  cursor = null,
  limit = 50,
  signal,
}) {
  const values = { cursor, limit };
  if (status) values.status = normalizedEnum(status, INVOCATION_STATUSES);
  if (serverId) values.server_id = requireId(serverId, SERVER_ID);
  if (toolId) values.tool_id = requireId(toolId, TOOL_ID);
  const query = boundedListQuery(values, INVOCATION_STATUSES);
  return mcpRequest(`${controlApiUrl}/mcp/invocations${query}`, { token, signal });
}

/** Generate a visible-ASCII idempotency key that contains no tenant, endpoint, token or business value. */
export function createMcpIdempotencyKey(kind = "operation") {
  idempotencySequence += 1;
  const normalizedKind = /^[a-z]{3,16}$/.test(kind) ? kind : "operation";
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    ?? Math.random().toString(36).slice(2).padEnd(16, "0");
  return `mcp_${normalizedKind}_${Date.now()}_${idempotencySequence}_${random.slice(0, 24)}`;
}

/** Normalize the policy form into the server's closed request shape. */
export function normalizeMcpToolPolicy(value = {}) {
  const expectedRevision = positiveInteger(value.expected_policy_revision);
  const status = normalizedEnum(value.status, TOOL_STATUSES);
  const riskLevel = normalizedEnum(value.risk_level, RISK_LEVELS);
  const approvalRequired = value.approval_required;
  const expires = positiveInteger(value.approval_expires_seconds);
  if (
    typeof approvalRequired !== "boolean"
    || expires < 300
    || expires > 86400
    || (riskLevel === "high" && !approvalRequired)
  ) throw localError("mcp.policy_invalid", 422);
  const roleIds = normalizeIds(value.allowed_role_ids);
  const userIds = normalizeIds(value.allowed_user_ids);
  if (status === "active" && roleIds.length + userIds.length === 0) {
    throw localError("mcp.policy_invalid", 422);
  }
  return Object.freeze({
    expected_policy_revision: expectedRevision,
    status,
    risk_level: riskLevel,
    approval_required: approvalRequired,
    approval_expires_seconds: expires,
    required_permission_codes: normalizeCodes(value.required_permission_codes, 8),
    allowed_role_ids: roleIds,
    allowed_user_ids: userIds,
    result_redaction_pointers: normalizePointers(value.result_redaction_pointers),
  });
}

/** Validate and freeze the only accepted registration fields. */
export function normalizeServerRegistration(value = {}) {
  const name = String(value.name ?? "").trim();
  const endpointUrl = String(value.endpointUrl ?? "").trim();
  const authType = String(value.authType ?? "none").trim();
  const bearerToken = String(value.bearerToken ?? "");
  if (
    !name
    || name.length > 80
    || /[\u0000-\u001f\u007f]/u.test(name)
    || !endpointUrl
    || endpointUrl.length > 2048
    || /[\u0000-\u001f\u007f]/u.test(endpointUrl)
    || !["none", "bearer"].includes(authType)
    || (authType === "bearer" && (!bearerToken || bearerToken.length > 8192))
    || (authType === "none" && bearerToken)
  ) throw localError("mcp.server_request_invalid", 422);
  return Object.freeze({
    name,
    endpoint_url: endpointUrl,
    auth: authType === "bearer"
      ? Object.freeze({ type: "bearer", bearer_token: bearerToken })
      : Object.freeze({ type: "none" }),
  });
}

async function mcpRequest(url, {
  token,
  method = "GET",
  body,
  idempotencyKey,
  signal,
}) {
  if (!String(token ?? "")) throw localError("auth.unauthorized", 401);
  const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw localError("mcp.unavailable", 503);
  }
  const payload = await safeJson(response);
  if (!response.ok) {
    throw new AuthApiError({
      status: response.status,
      code: ERROR_CODE.test(String(payload.code ?? "")) ? payload.code : "mcp.unavailable",
      message: "MCP 请求未完成",
      requestId: safeId(payload.request_id) || safeId(response.headers?.get?.("X-Request-ID")) || "-",
      traceId: safeId(payload.trace_id) || safeId(response.headers?.get?.("X-Trace-ID")) || "-",
      suggestion: "",
      diagnosis: null,
    });
  }
  return payload;
}

function boundedListQuery(values, statuses) {
  const params = new URLSearchParams();
  const limit = Number(values.limit ?? 50);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
    throw localError("mcp.query_invalid", 422);
  }
  params.set("limit", String(limit));
  if (values.status) params.set("status", normalizedEnum(values.status, statuses));
  if (values.server_id) params.set("server_id", values.server_id);
  if (values.tool_id) params.set("tool_id", values.tool_id);
  if (values.risk) params.set("risk", values.risk);
  if (values.approval_required !== undefined) {
    params.set("approval_required", values.approval_required);
  }
  if (values.cursor) {
    const cursor = String(values.cursor);
    if (!SAFE_ID.test(cursor) || cursor.length > 512) throw localError("mcp.query_invalid", 422);
    params.set("cursor", cursor);
  }
  return `?${params.toString()}`;
}

function normalizeCodes(value, maximum) {
  if (!Array.isArray(value) || value.length > maximum) throw localError("mcp.policy_invalid", 422);
  const normalized = value.map((item) => String(item ?? "").trim()).filter(Boolean).sort();
  if (
    normalized.some((item) => !ERROR_CODE.test(item))
    || new Set(normalized).size !== normalized.length
  ) throw localError("mcp.policy_invalid", 422);
  return normalized;
}

function normalizeIds(value) {
  if (!Array.isArray(value) || value.length > 100) throw localError("mcp.policy_invalid", 422);
  const normalized = value.map((item) => String(item ?? "").trim()).filter(Boolean).sort();
  if (normalized.some((item) => !SAFE_ID.test(item)) || new Set(normalized).size !== normalized.length) {
    throw localError("mcp.policy_invalid", 422);
  }
  return normalized;
}

function normalizePointers(value) {
  if (!Array.isArray(value) || value.length > 32) throw localError("mcp.policy_invalid", 422);
  const normalized = value.map((item) => String(item ?? "")).sort();
  if (
    normalized.some((item) => item.length > 512 || !/^(?:\/(?:[^~]|~[01])*)*$/u.test(item))
    || new Set(normalized).size !== normalized.length
  ) throw localError("mcp.policy_invalid", 422);
  return normalized;
}

function requireId(value, pattern) {
  const normalized = String(value ?? "").trim();
  if (!pattern.test(normalized)) throw localError("mcp.not_found", 404);
  return normalized;
}

function positiveInteger(value) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 1) throw localError("mcp.request_invalid", 422);
  return number;
}

function normalizedEnum(value, allowed) {
  const normalized = String(value ?? "").trim();
  if (!allowed?.has(normalized)) throw localError("mcp.query_invalid", 422);
  return normalized;
}

function validIdempotencyKey(value) {
  const normalized = String(value ?? "");
  if (normalized.length < 8 || normalized.length > 128 || !/^[\x21-\x7e]+$/.test(normalized)) {
    throw localError("mcp.idempotency_key_invalid", 422);
  }
  return normalized;
}

function safeId(value) {
  const normalized = String(value ?? "").trim();
  return SAFE_ID.test(normalized) ? normalized : "";
}

async function safeJson(response) {
  try {
    const payload = await response.json();
    return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return {};
  }
}

function localError(code, status) {
  return new AuthApiError({
    status,
    code,
    message: "MCP 请求不合法",
    requestId: "-",
    traceId: "-",
    suggestion: "",
    diagnosis: null,
  });
}
