// PRD-088 白标客户端：严格校验品牌 DTO，并把图片正文限制在短生命周期 Blob 内。
import { AuthApiError } from "./auth.js";

const BRANDING_KEYS = Object.freeze([
  "branding_config_id", "product_name", "primary_color", "secondary_color",
  "login_title", "login_message", "custom_domain", "custom_domain_status",
  "logo_present", "logo_sha256", "logo_width", "logo_height", "revision",
  "status", "updated_at",
]);
const ENVELOPE_KEYS = Object.freeze(["branding", "request_id", "trace_id"]);
const PUBLIC_KEYS = Object.freeze([
  "product_name", "primary_color", "secondary_color", "login_title",
  "login_message", "logo_present",
]);
const PUBLIC_ENVELOPE_KEYS = Object.freeze(["branding", "request_id", "trace_id"]);
const PATCH_KEYS = Object.freeze([
  "product_name", "primary_color", "secondary_color", "login_title",
  "login_message", "custom_domain", "logo_png_base64",
]);
const COLOR = /^#[0-9A-F]{6}$/;
const BRANDING_ID = /^brc_[0-9a-f]{32}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const SLUG = /^[a-z0-9][a-z0-9_-]{0,79}$/;
const DOMAIN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;
const UTC_SECOND = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
// Control-plane accepts bare RFC 4648 base64 and caps PNGs at 128 KiB.
const LOGO_BASE64 = /^(?=.+)(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const MAX_LOGO_BYTES = 128 * 1024;
const PNG_SIGNATURE = Object.freeze([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const FIXED_ERRORS = Object.freeze({
  "branding.request_invalid": "白标请求不合法",
  "branding.not_found": "白标配置不存在",
  "branding.logo_not_configured": "当前租户未配置 Logo",
  "branding.logo_invalid": "Logo 必须是有效的 PNG 图片",
  "branding.logo_too_large": "Logo 超过大小限制",
  "branding.custom_domain_invalid": "自定义域名不合法",
  "branding.custom_domain_conflict": "自定义域名已被占用",
  "branding.revision_conflict": "白标配置已被更新，请刷新后重试",
  "branding.forbidden": "当前账号没有白标配置权限",
  "branding.unavailable": "白标服务暂不可用",
  "branding.response_invalid": "白标响应不符合安全合同",
});

/** 读取当前租户的受保护 BrandingConfig；tenant_id 只由后端 Session 恢复。 */
export async function getBranding({ controlApiUrl, token, signal }) {
  const payload = await brandingRequest(`${controlApiUrl}/branding`, {
    headers: authorizationHeaders(token), signal,
  });
  const normalized = normalizeBrandingResponse(payload);
  if (!normalized) throw brandingError("branding.response_invalid", 502, payload?.request_id);
  return normalized;
}

/** 以十进制 revision CAS 更新白标；请求不得夹带 tenant_id、actor 或 Logo URL。 */
export async function updateBranding({ controlApiUrl, token, input, revision, signal }) {
  const body = normalizeBrandingPatch(input);
  const expectedRevision = requirePositiveInteger(revision, "revision");
  const payload = await brandingRequest(`${controlApiUrl}/branding`, {
    method: "PATCH",
    headers: {
      ...authorizationHeaders(token),
      "Content-Type": "application/json",
      "If-Match": String(expectedRevision),
    },
    body: JSON.stringify(body), signal,
  });
  const normalized = normalizeBrandingResponse(payload);
  if (!normalized) throw brandingError("branding.response_invalid", 502, payload?.request_id);
  return normalized;
}

/** 登录前仅按租户 slug 读取公开品牌投影；响应不能推导 tenant_id 或权限。 */
export async function getPublicBranding({ controlApiUrl, tenantSlug, signal }) {
  const slug = normalizeTenantSlug(tenantSlug);
  const url = new URL(`${controlApiUrl}/branding/public`);
  url.searchParams.set("tenant_slug", slug);
  const payload = await brandingRequest(url.toString(), { signal });
  const normalized = normalizePublicBrandingResponse(payload);
  if (!normalized) throw brandingError("branding.response_invalid", 502, payload?.request_id);
  return normalized;
}

/** 读取受保护同源 PNG Logo；正文只返回 Blob，不写入 state、日志或响应证据。 */
export async function getBrandingLogo({ controlApiUrl, token, signal }) {
  return logoRequest(`${controlApiUrl}/branding/logo`, {
    headers: authorizationHeaders(token), signal,
  });
}

/** 读取登录页同源公开 PNG Logo；tenant slug 仅进入受限 query 参数。 */
export async function getPublicBrandingLogo({ controlApiUrl, tenantSlug, signal }) {
  const slug = normalizeTenantSlug(tenantSlug);
  const url = new URL(`${controlApiUrl}/branding/public/logo`);
  url.searchParams.set("tenant_slug", slug);
  return logoRequest(url.toString(), { signal });
}

/** 把短生命周期 PNG Blob 转为对象 URL；调用方必须在 owner 清理时 revoke。 */
export function createBrandingObjectUrl(blob, urlApi = globalThis.URL) {
  const blobType = typeof Blob === "undefined" ? null : Blob;
  if (!blobType || !(blob instanceof blobType) || blob.type !== "image/png" || blob.size < 1 || blob.size > MAX_LOGO_BYTES) {
    throw brandingError("branding.response_invalid", 502, "-");
  }
  if (typeof urlApi?.createObjectURL !== "function") return "";
  return urlApi.createObjectURL(blob);
}

/** 统一回收白标 Logo 对象 URL，避免刷新、撤权或登出后跨租户复用图片。 */
export function revokeBrandingObjectUrl(url, urlApi = globalThis.URL) {
  if (typeof url === "string" && url.startsWith("blob:") && typeof urlApi?.revokeObjectURL === "function") {
    urlApi.revokeObjectURL(url);
  }
}

/** Renderers only accept object URLs created from validated PNG Blobs. */
export function safeBrandingLogoUrl(value) {
  const url = String(value ?? "");
  return /^blob:[^\s]+$/i.test(url) ? url : "";
}

/** 受保护 DTO 严格归一化，拒绝 tenant_id、Logo 正文及未知字段进入展示状态。 */
export function normalizeBrandingResponse(payload) {
  if (!hasExactKeys(payload, ENVELOPE_KEYS) || !hasExactKeys(payload?.branding, BRANDING_KEYS)) return null;
  const value = payload.branding;
  if (!BRANDING_ID.test(String(value.branding_config_id ?? ""))) return null;
  if (!boundedText(value.product_name, 1, 80) || !validColor(value.primary_color) || !validColor(value.secondary_color)) return null;
  if (!boundedText(value.login_title, 1, 120) || !boundedText(value.login_message, 1, 240)) return null;
  if (value.custom_domain !== null && !validDomain(value.custom_domain)) return null;
  if ((value.custom_domain === null) !== (value.custom_domain_status === "not_configured")) return null;
  if ((value.custom_domain !== null) !== (value.custom_domain_status === "reserved")) return null;
  if (typeof value.logo_present !== "boolean") return null;
  if (value.logo_present && (!SHA256.test(String(value.logo_sha256 ?? "")) || !logoDimension(value.logo_width) || !logoDimension(value.logo_height))) return null;
  if (!value.logo_present && (value.logo_sha256 !== null || value.logo_width !== null || value.logo_height !== null)) return null;
  if (!positiveInteger(value.revision) || value.status !== "active") return null;
  if (!UTC_SECOND.test(String(value.updated_at ?? ""))) return null;
  if (safeChain(payload.request_id) === "-" || safeChain(payload.trace_id) === "-") return null;
  return Object.freeze({ branding: Object.freeze({ ...value }), request_id: safeChain(payload.request_id), trace_id: safeChain(payload.trace_id) });
}

/** 登录前公开投影严格白名单，绝不把 revision/status/tenant_id 传播到登录 DOM。 */
export function normalizePublicBrandingResponse(payload) {
  if (!hasExactKeys(payload, PUBLIC_ENVELOPE_KEYS) || !hasExactKeys(payload?.branding, PUBLIC_KEYS)) return null;
  const value = payload.branding;
  if (!boundedText(value.product_name, 1, 80) || !validColor(value.primary_color) || !validColor(value.secondary_color)) return null;
  if (!boundedText(value.login_title, 1, 120) || !boundedText(value.login_message, 1, 240) || typeof value.logo_present !== "boolean") return null;
  if (safeChain(payload.request_id) === "-" || safeChain(payload.trace_id) === "-") return null;
  return Object.freeze({ branding: Object.freeze({ ...value }), request_id: safeChain(payload.request_id), trace_id: safeChain(payload.trace_id) });
}

/** 只允许 API 白名单字段；logo_png_base64 必须是裸 PNG base64 内容或 null。 */
/** Client wire format mirrors BrandingPatchRequest: bare base64, never a data URL. */
export function normalizeBrandingPatch(input = {}) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw brandingError("branding.request_invalid", 422, "-");
  const output = {};
  for (const key of Object.keys(input)) if (!PATCH_KEYS.includes(key)) throw brandingError("branding.request_invalid", 422, "-");
  if (input.product_name !== undefined && !boundedText(input.product_name, 1, 80)) throw brandingError("branding.request_invalid", 422, "-");
  if (input.login_title !== undefined && !boundedText(input.login_title, 1, 120)) throw brandingError("branding.request_invalid", 422, "-");
  if (input.login_message !== undefined && !boundedText(input.login_message, 1, 240)) throw brandingError("branding.request_invalid", 422, "-");
  for (const key of ["primary_color", "secondary_color"]) {
    if (input[key] !== undefined && !validPatchColor(input[key])) throw brandingError("branding.request_invalid", 422, "-");
  }
  if (input.custom_domain !== undefined && input.custom_domain !== null && input.custom_domain !== "" && !validDomain(input.custom_domain)) throw brandingError("branding.request_invalid", 422, "-");
  if (input.logo_png_base64 !== undefined && input.logo_png_base64 !== null && (typeof input.logo_png_base64 !== "string" || !LOGO_BASE64.test(input.logo_png_base64) || decodedBase64Bytes(input.logo_png_base64) > MAX_LOGO_BYTES)) throw brandingError("branding.request_invalid", 422, "-");
  for (const key of PATCH_KEYS) {
    if (input[key] === undefined) continue;
    if (key === "custom_domain" && input[key] === "") output[key] = null;
    else if (key === "primary_color" || key === "secondary_color") output[key] = input[key].toUpperCase();
    else output[key] = input[key];
  }
  if (!Object.keys(output).length) throw brandingError("branding.request_invalid", 422, "-");
  return output;
}

/** 将本地 PNG File 转为受限裸 base64；File 不进入长期 state 或日志。 */
export async function pngFileToBase64(file) {
  const blobType = typeof Blob === "undefined" ? null : Blob;
  if (!blobType || !(file instanceof blobType) || file.size < 1 || file.size > MAX_LOGO_BYTES || file.type !== "image/png") {
    throw brandingError("branding.request_invalid", 422, "-");
  }
  const buffer = await file.arrayBuffer();
  if (!hasPngSignature(new Uint8Array(buffer))) throw brandingError("branding.request_invalid", 422, "-");
  let binary = "";
  for (const byte of new Uint8Array(buffer)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

// Source-compatible alias retained for the initial PAGE-120 UI.
export const pngFileToDataUrl = pngFileToBase64;

function authorizationHeaders(token) { return { Authorization: `Bearer ${String(token ?? "")}` }; }

async function brandingRequest(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "X-Request-ID": `console_branding_${Date.now()}`, ...(options.headers ?? {}) } });
  const payload = await readJson(response);
  if (!response.ok) throw brandingError(payload.code, response.status, payload.request_id || response.headers.get("X-Request-ID"));
  return payload;
}

async function logoRequest(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "X-Request-ID": `console_branding_logo_${Date.now()}`, ...(options.headers ?? {}) } });
  if (!response.ok) {
    const payload = await readJson(response);
    throw brandingError(payload.code, response.status, payload.request_id || response.headers.get("X-Request-ID"));
  }
  const contentType = String(response.headers.get("Content-Type") ?? "").toLowerCase().split(";", 1)[0];
  if (contentType !== "image/png") throw brandingError("branding.response_invalid", 502, response.headers.get("X-Request-ID"));
  const blob = await response.blob();
  const blobType = typeof Blob === "undefined" ? null : Blob;
  if (!blobType || !(blob instanceof blobType) || blob.type !== "image/png" || blob.size < 1 || blob.size > MAX_LOGO_BYTES) throw brandingError("branding.response_invalid", 502, response.headers.get("X-Request-ID"));
  const signature = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
  if (!hasPngSignature(signature)) {
    throw brandingError("branding.response_invalid", 502, response.headers.get("X-Request-ID"));
  }
  return { blob, request_id: safeChain(response.headers.get("X-Request-ID")) };
}

function hasExactKeys(value, keys) { return Boolean(value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key))); }
function validColor(value) { return COLOR.test(String(value ?? "")); }
function validPatchColor(value) { return /^#[0-9a-f]{6}$/i.test(String(value ?? "")); }
function positiveInteger(value) { return Number.isSafeInteger(value) && value >= 1; }
function logoDimension(value) { return Number.isInteger(value) && value >= 16 && value <= 512; }
function requirePositiveInteger(value, field) { if (!positiveInteger(value)) throw brandingError("branding.request_invalid", 422, "-"); return Number(value); }
function boundedText(value, min, max) { return typeof value === "string" && value.length >= min && new TextEncoder().encode(value).length <= max && !/[\u0000-\u001f\u007f]/u.test(value); }
function normalizeTenantSlug(value) { const slug = String(value ?? "").trim().toLowerCase(); if (!SLUG.test(slug)) throw brandingError("branding.request_invalid", 422, "-"); return slug; }
function validDomain(value) {
  if (typeof value !== "string" || value !== value.toLowerCase() || value.length > 253 || !DOMAIN.test(value)) return false;
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(value)) return false;
  if (["example.com", "example.cn"].includes(value)) return false;
  return ![".example", ".invalid", ".internal", ".local", ".localhost", ".test"].some((suffix) => value.endsWith(suffix));
}
function decodedBase64Bytes(value) { const encoded = String(value); return Math.floor((encoded.length * 3) / 4) - (encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0); }
function hasPngSignature(value) { return value.length >= PNG_SIGNATURE.length && PNG_SIGNATURE.every((byte, index) => value[index] === byte); }
function safeChain(value) { return /^[A-Za-z0-9_.:-]{1,160}$/.test(String(value ?? "")) ? String(value) : "-"; }
function brandingError(code, status, requestId) { const normalized = Object.hasOwn(FIXED_ERRORS, code) ? code : "branding.unavailable"; return new AuthApiError({ status: Number(status) || 502, code: normalized, message: FIXED_ERRORS[normalized], requestId: safeChain(requestId), traceId: "-" }); }
async function readJson(response) { try { return await response.json(); } catch { return {}; } }
