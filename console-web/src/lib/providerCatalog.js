import { AuthApiError } from "./auth.js";

const ENTRY_CODE = /^[a-z][a-z0-9_]{1,63}$/u;
const VERSION = /^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,3}$/u;
const REGION_CODE = /^[a-z0-9][a-z0-9-]{1,39}$/u;
const PROVIDER_CODE = /^[a-z][a-z0-9_]{1,63}$/u;
const CAPABILITY_CODE = /^[a-z][a-z0-9_]{1,63}$/u;
const FILTER_CODE = /^[a-z0-9][a-z0-9_-]{1,63}$/u;
const PROVIDER_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{2,159}$/u;

export async function listProviderCatalog({
  controlApiUrl,
  token,
  filters = {},
  signal,
} = {}) {
  const url = new URL(`${controlApiUrl}/provider-catalog`);
  const values = {
    status: catalogFilter(filters.status, new Set(["active", "deprecated", "retired"])),
    vendor_code: catalogFilter(filters.vendorCode),
    capability_code: catalogFilter(filters.capabilityCode),
    region_code: catalogFilter(filters.regionCode),
  };
  for (const [key, value] of Object.entries(values)) {
    if (value) url.searchParams.set(key, value);
  }
  return catalogRequest(url.toString(), {
    headers: { Authorization: `Bearer ${requiredToken(token)}` },
    signal,
  });
}

export async function getProviderCatalogCompatibility({
  controlApiUrl,
  token,
  entryCode,
  signal,
} = {}) {
  return catalogRequest(
    `${controlApiUrl}/provider-catalog/${encodeURIComponent(code(entryCode, ENTRY_CODE))}/compatibility`,
    { headers: { Authorization: `Bearer ${requiredToken(token)}` }, signal },
  );
}

export async function createProviderFromCatalog({
  controlApiUrl,
  token,
  entryCode,
  version,
  regionCode,
  capabilityCodes,
  providerCode,
  name,
  confirmed,
  idempotencyKey = createProviderCatalogIdempotencyKey("instantiate"),
  signal,
} = {}) {
  const normalizedCapabilities = canonicalCodes(capabilityCodes);
  const displayName = String(name ?? "").trim();
  if (displayName.length < 2 || displayName.length > 120 || confirmed !== true) invalidRequest();
  return catalogRequest(`${controlApiUrl}/providers/from-catalog`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${requiredToken(token)}`,
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKeyValue(idempotencyKey),
    },
    body: JSON.stringify({
      entry_code: code(entryCode, ENTRY_CODE),
      version: code(version, VERSION),
      region_code: code(regionCode, REGION_CODE),
      capability_codes: normalizedCapabilities,
      provider_code: code(providerCode, PROVIDER_CODE),
      name: displayName,
      confirmed: true,
    }),
    signal,
  });
}

export async function preflightCatalogProvider({
  controlApiUrl,
  token,
  providerId,
  timeoutSeconds = 5,
  idempotencyKey = createProviderCatalogIdempotencyKey("preflight"),
  signal,
} = {}) {
  const timeout = Number(timeoutSeconds);
  if (!Number.isFinite(timeout) || timeout < 0.5 || timeout > 25) invalidRequest();
  return catalogRequest(
    `${controlApiUrl}/providers/${encodeURIComponent(code(providerId, PROVIDER_ID))}/preflight`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${requiredToken(token)}`,
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKeyValue(idempotencyKey),
      },
      body: JSON.stringify({ timeout_seconds: timeout }),
      signal,
    },
  );
}

export function createProviderCatalogIdempotencyKey(kind = "request") {
  const safeKind = new Set(["instantiate", "preflight"]).has(kind) ? kind : "request";
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
  if (!random) {
    throw new AuthApiError({
      status: 0,
      code: "provider_catalog.configuration_invalid",
      message: "当前环境无法生成安全请求标识",
      requestId: "-",
      traceId: "-",
    });
  }
  return `console:provider-catalog:${safeKind}:${random}`;
}

async function catalogRequest(url, options = {}) {
  const requestId = `console_provider_catalog_${Date.now()}`;
  const response = await fetch(url, {
    ...options,
    headers: { "X-Request-ID": requestId, ...(options.headers ?? {}) },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new AuthApiError({
      status: response.status,
      code: payload.code || "provider_catalog.unavailable",
      message: payload.message || `Provider Catalog 接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || requestId,
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

function catalogFilter(value, allowed) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return "";
  if ((allowed && !allowed.has(normalized)) || (!allowed && !FILTER_CODE.test(normalized))) {
    invalidRequest();
  }
  return normalized;
}

function canonicalCodes(values) {
  if (!Array.isArray(values) || values.length < 1 || values.length > 32) invalidRequest();
  const normalized = values.map((value) => code(value, CAPABILITY_CODE)).sort();
  if (new Set(normalized).size !== normalized.length) invalidRequest();
  return normalized;
}

function code(value, pattern) {
  const normalized = String(value ?? "").trim();
  if (!pattern.test(normalized)) invalidRequest();
  return normalized;
}

function requiredToken(value) {
  const token = String(value ?? "");
  if (!token) invalidRequest();
  return token;
}

function idempotencyKeyValue(value) {
  const normalized = String(value ?? "");
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u.test(normalized)) invalidRequest();
  return normalized;
}

function invalidRequest() {
  throw new AuthApiError({
    status: 422,
    code: "provider_catalog.request_invalid",
    message: "Provider Catalog 请求字段无效",
    requestId: "-",
    traceId: "-",
  });
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
