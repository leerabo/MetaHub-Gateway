const GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf";
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LOCALES = new Set(["zh-CN", "en-US"]);

export class AccountPreferenceError extends Error {
  constructor({ status = 0, code = "identity.preference.request_failed", message, requestId = "-", traceId = "-" } = {}) {
    super(safeText(message) || "账号偏好请求未完成。");
    this.name = "AccountPreferenceError";
    this.status = Number(status) || 0;
    this.code = safeCode(code);
    this.requestId = safeChain(requestId);
    this.traceId = safeChain(traceId);
  }
}

export function getAccountPreferences({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/identity/preferences", {
    signal,
    normalize: normalizeAccountPreferences,
  });
}

export function updateAccountPreferences({
  controlApiUrl,
  locale,
  revision,
  idempotencyKey = createAccountPreferenceIdempotencyKey(),
  signal,
} = {}) {
  if (!LOCALES.has(locale) || !Number.isSafeInteger(revision) || revision < 1
    || !IDEMPOTENCY_KEY.test(String(idempotencyKey))) invalidRequest();
  return request(controlApiUrl, "/identity/preferences", {
    method: "PATCH",
    body: { locale },
    headers: {
      "If-Match": `"${revision}"`,
      "Idempotency-Key": String(idempotencyKey),
      "X-CSRF-Token": readExactCookie(GLOBAL_CSRF_COOKIE),
    },
    signal,
    normalize: normalizeAccountPreferenceCommand,
  });
}

export function createAccountPreferenceIdempotencyKey() {
  if (!globalThis.crypto?.getRandomValues) {
    throw new AccountPreferenceError({
      code: "identity.preference.configuration_invalid",
      message: "当前环境无法生成安全操作标识。",
    });
  }
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return `account-preference:${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

export function normalizeAccountPreferences(value) {
  exact(value, [
    "available_locales", "confirmation_state", "locale", "preference_id", "request_id",
    "revision", "trace_id", "updated_at",
  ]);
  return normalizeBase(value);
}

export function normalizeAccountPreferenceCommand(value) {
  exact(value, [
    "available_locales", "changed", "confirmation_state", "idempotent_replay", "locale",
    "preference_id", "request_id", "revision", "trace_id", "updated_at",
  ]);
  if (typeof value.changed !== "boolean" || typeof value.idempotent_replay !== "boolean") invalidResponse();
  return deepFreeze({
    ...normalizeBase(value),
    changed: value.changed,
    idempotent_replay: value.idempotent_replay,
  });
}

async function request(controlApiUrl, path, options) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(options.headers ?? {}),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new AccountPreferenceError({
      code: "identity.preference.network_unavailable",
      message: "暂时无法连接账号偏好服务。",
    });
  }
  const payload = await json(response);
  if (!response.ok) {
    throw new AccountPreferenceError({
      status: response.status,
      code: payload?.code,
      message: payload?.message,
      requestId: payload?.request_id,
      traceId: payload?.trace_id,
    });
  }
  const normalized = options.normalize(payload);
  requirePrivateResponse(response, normalized.revision);
  return normalized;
}

function normalizeBase(value) {
  if (!LOCALES.has(value.locale)
    || !Array.isArray(value.available_locales)
    || value.available_locales.length < 1
    || value.available_locales.length > 2
    || value.available_locales.some((locale) => !LOCALES.has(locale))
    || new Set(value.available_locales).size !== value.available_locales.length
    || !value.available_locales.includes(value.locale)
    || !new Set(["confirmed", "needs_confirmation"]).has(value.confirmation_state)
    || !Number.isSafeInteger(value.revision)
    || value.revision < 1) invalidResponse();
  return deepFreeze({
    ...value,
    preference_id: boundedText(value.preference_id, 1, 160),
    available_locales: [...value.available_locales],
    updated_at: timestamp(value.updated_at),
    request_id: chain(value.request_id),
    trace_id: chain(value.trace_id),
  });
}

function requirePrivateResponse(response, revision) {
  const cacheControl = response.headers?.get?.("cache-control")?.toLowerCase() ?? "";
  const pragma = response.headers?.get?.("pragma")?.toLowerCase() ?? "";
  const vary = response.headers?.get?.("vary")?.toLowerCase() ?? "";
  const etag = response.headers?.get?.("etag") ?? "";
  if (!cacheControl.includes("no-store") || !cacheControl.includes("private")
    || pragma !== "no-cache" || !vary.split(",").map((item) => item.trim()).includes("cookie")
    || etag !== `"${revision}"`) invalidResponse();
}

function apiUrl(controlApiUrl, path) {
  try {
    const base = new URL(String(controlApiUrl));
    if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) throw new Error();
    return new URL(path, base).toString();
  } catch {
    throw new AccountPreferenceError({ code: "identity.preference.configuration_invalid", message: "账号服务地址无效。" });
  }
}

function readExactCookie(name, cookieText = globalThis.document?.cookie ?? "") {
  const values = String(cookieText).split(";").map((item) => item.trim())
    .filter((item) => item.startsWith(`${name}=`)).map((item) => item.slice(name.length + 1));
  if (values.length !== 1) invalidRequest("identity.preference.csrf_invalid");
  const value = decodeURIComponent(values[0]);
  if (!/^[A-Za-z0-9_-]{43,128}$/u.test(value)) invalidRequest("identity.preference.csrf_invalid");
  return value;
}

function exact(value, keys) {
  if (!plain(value) || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) invalidResponse();
  if (Object.keys(value).some((key) => /^(?:account_id|authorization|cookie|csrf|password|secret|tenant_id|token|workspace_id)$/iu.test(key))) invalidResponse();
}
function boundedText(value, minimum, maximum) { if (typeof value !== "string" || value.length < minimum || value.length > maximum || /[\u0000-\u001f\u007f-\u009f]/u.test(value)) invalidResponse(); return value; }
function timestamp(value) { const text = boundedText(value, 20, 40); if (!Number.isFinite(Date.parse(text))) invalidResponse(); return text; }
function chain(value) { const text = boundedText(value, 1, 160); if (!/^[A-Za-z0-9_.:-]+$/u.test(text)) invalidResponse(); return text; }
function plain(value) { return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype; }
function safeText(value) { return typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f-\u009f]/gu, " ").slice(0, 240) : ""; }
function safeCode(value) { const text = String(value ?? ""); return /^(?:identity|account_preference)\.[a-z0-9_.]{1,120}$/u.test(text) ? text : "identity.preference.request_failed"; }
function safeChain(value) { const text = String(value ?? ""); return /^[A-Za-z0-9_.:-]{1,160}$/u.test(text) ? text : "-"; }
function deepFreeze(value) { if (value && typeof value === "object" && !Object.isFrozen(value)) { Object.freeze(value); for (const child of Object.values(value)) deepFreeze(child); } return value; }
function invalidRequest(code = "identity.preference.request_invalid") { throw new AccountPreferenceError({ code, message: "账号偏好请求字段无效。" }); }
function invalidResponse() { throw new AccountPreferenceError({ status: 502, code: "identity.preference.response_invalid", message: "账号偏好响应格式无效。" }); }
async function json(response) { try { const value = await response.json(); return plain(value) ? value : {}; } catch { return {}; } }
