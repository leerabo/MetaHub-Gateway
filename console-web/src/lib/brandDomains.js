/** Strict PRD-142 clients for owner-separated brand domains. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const IDS = Object.freeze({
  platform: /^pbr_[0-9a-f]{32}$/u,
  channel: /^cbr_[0-9a-f]{32}$/u,
  deployment: /^dbr_[0-9a-f]{32}$/u,
  partner: /^par_[0-9a-f]{32}$/u,
  edgeDeployment: /^edp_[0-9a-f]{32}$/u,
  asset: /^bas_[0-9a-f]{32}$/u,
  domain: /^vdo_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const CHAIN = /^[A-Za-z0-9_.:-]{1,160}$/u;
const OPAQUE = /^[A-Za-z0-9_-]{43,128}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const COLOR = /^#[0-9A-F]{6}$/u;
const PUBLIC_ASSET = /^\/branding\/public\/assets\/bas_[0-9a-f]{32}$/u;
const VISUAL_KEYS = new Set([
  "product_name", "primary_color", "secondary_color", "login_title", "login_message",
]);
const COMMON_ACTIONS = [
  "update", "attach_asset", "remove_asset", "suspend", "rollback_lkg",
];

export async function getPlatformBranding({ controlApiUrl, signal } = {}) {
  return protectedRequest(controlApiUrl, "/platform/branding", {
    domain: "platform", kind: "platform", signal,
  });
}

export async function updatePlatformBranding({
  controlApiUrl, command, expectedRevision, stepUpGrant, idempotencyKey, signal,
} = {}) {
  return protectedRequest(controlApiUrl, "/platform/branding", {
    domain: "platform",
    kind: "platform",
    method: "PATCH",
    body: brandCommand(command, "platform"),
    expectedRevision,
    stepUpGrant,
    idempotencyKey,
    signal,
  });
}

export async function getChannelBranding({ controlApiUrl, partnerId, signal } = {}) {
  const id = requestPattern(partnerId, IDS.partner, "channel_partner_id", true);
  return protectedRequest(
    controlApiUrl,
    `/platform/channel-partners/${encodeURIComponent(id)}/branding`,
    { domain: "platform", kind: "channel", partnerId: id, signal },
  );
}

export async function updateChannelBranding({
  controlApiUrl, partnerId, command, expectedRevision, stepUpGrant, idempotencyKey, signal,
} = {}) {
  const id = requestPattern(partnerId, IDS.partner, "channel_partner_id", true);
  return protectedRequest(
    controlApiUrl,
    `/platform/channel-partners/${encodeURIComponent(id)}/branding`,
    {
      domain: "platform",
      kind: "channel",
      partnerId: id,
      method: "PATCH",
      body: brandCommand(command, "channel"),
      expectedRevision,
      stepUpGrant,
      idempotencyKey,
      signal,
    },
  );
}

export async function getDeploymentBranding({ controlApiUrl, deploymentId, signal } = {}) {
  const id = requestPattern(deploymentId, IDS.edgeDeployment, "edge_deployment_id", false);
  return protectedRequest(
    controlApiUrl,
    `/deployments/${encodeURIComponent(id)}/branding`,
    { domain: "workspace", kind: "deployment", deploymentId: id, signal },
  );
}

export async function updateDeploymentBranding({
  controlApiUrl, deploymentId, command, expectedRevision, stepUpGrant, idempotencyKey, signal,
} = {}) {
  const id = requestPattern(deploymentId, IDS.edgeDeployment, "edge_deployment_id", false);
  return protectedRequest(
    controlApiUrl,
    `/deployments/${encodeURIComponent(id)}/branding`,
    {
      domain: "workspace",
      kind: "deployment",
      deploymentId: id,
      method: "PATCH",
      body: brandCommand(command, "deployment"),
      expectedRevision,
      stepUpGrant,
      idempotencyKey,
      signal,
    },
  );
}

export async function getPublicBranding({ controlApiUrl, signal } = {}) {
  const response = await fetchJson(controlApiUrl, "/branding/public", {
    domain: "public", signal,
  });
  exact(response.payload, ["branding", "request_id", "trace_id"], "public", false);
  const branding = publicBrand(response.payload.branding);
  const cache = cacheDirectives(response.response);
  const etag = response.response.headers.get("ETag");
  if (!cache.has("no-store") || etag !== branding.public_etag) invalidResponse("public", "boundary");
  return freeze({ branding, ...trace(response.payload, "public") });
}

export function createBrandingIdempotencyKey(scope = "brand") {
  const value = globalThis.crypto?.randomUUID?.();
  if (!value) invalidRequest("platform", "idempotency_key");
  return `${String(scope).replace(/[^A-Za-z0-9._:-]/gu, "-").slice(0, 40)}-${value}`;
}

export async function pngFileToBase64(file) {
  if (!file || typeof file.arrayBuffer !== "function" || file.type !== "image/png"
    || !Number.isSafeInteger(file.size) || file.size < 1 || file.size > 512 * 1024) {
    invalidRequest("workspace", "png");
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return globalThis.btoa(binary);
}

async function protectedRequest(controlApiUrl, path, options) {
  const response = await fetchJson(controlApiUrl, path, options);
  exact(
    response.payload,
    ["branding", "idempotent_replay", "request_id", "trace_id"],
    options.domain,
    options.method === "PATCH",
  );
  const branding = brandInfo(response.payload.branding, options.kind, options.domain);
  const cache = cacheDirectives(response.response);
  if (!cache.has("private") || !cache.has("no-store")
    || response.response.headers.get("ETag") !== `"${branding.revision}"`) {
    invalidResponse(options.domain, "boundary");
  }
  return freeze({
    branding,
    idempotent_replay: options.method === "PATCH"
      ? boolean(response.payload.idempotent_replay, "idempotent_replay", options.domain)
      : false,
    ...trace(response.payload, options.domain),
  });
}

async function fetchJson(controlApiUrl, path, options) {
  const headers = { Accept: "application/json" };
  let body;
  if (options.method === "PATCH") {
    const revision = requestInteger(options.expectedRevision, 1, "expected_revision", options.domain);
    headers["Content-Type"] = "application/json";
    headers["If-Match"] = `"${revision}"`;
    headers["Idempotency-Key"] = requestPattern(
      options.idempotencyKey, IDEMPOTENCY, "idempotency_key", options.domain === "platform",
    );
    headers["X-Step-Up-Grant"] = requestPattern(
      options.stepUpGrant, OPAQUE, "step_up_grant", options.domain === "platform",
    );
    headers["X-CSRF-Token"] = options.domain === "platform"
      ? readPlatformCsrfCookie() : readWorkspaceCsrfCookie();
    body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, options.domain), {
      method: options.method ?? "GET",
      headers,
      body,
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw domainError(options.domain, 0, "branding.unavailable", "Branding service unavailable");
  }
  const contentType = response.headers.get("Content-Type") ?? "";
  if (!/^application\/json(?:\s*;|$)/iu.test(contentType)) invalidResponse(options.domain, "content_type");
  let payload;
  try { payload = await response.json(); } catch { invalidResponse(options.domain, "json"); }
  plain(payload, "response", options.domain);
  if (!response.ok) {
    const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(payload.code ?? ""))
      ? payload.code : "branding.request_failed";
    throw domainError(
      options.domain,
      response.status,
      code,
      safeText(payload.message) || "Branding request failed",
      safeChain(payload.request_id),
    );
  }
  return { payload, response };
}

function brandCommand(value, kind) {
  plain(value, "command", kind === "deployment" ? "workspace" : "platform", true);
  const allowed = kind === "deployment"
    ? [...COMMON_ACTIONS, "publish"] : [...COMMON_ACTIONS, "request_domain", "release"];
  const action = requestChoice(value.action, allowed, "action", kind !== "deployment");
  const keys = Object.keys(value);
  if (action === "update") {
    if (keys.length < 2 || keys.some((key) => key !== "action" && !VISUAL_KEYS.has(key))) {
      invalidRequest(kind === "deployment" ? "workspace" : "platform", "update");
    }
    const command = { action };
    for (const key of keys.filter((key) => key !== "action")) {
      command[key] = key.endsWith("_color")
        ? requestPattern(value[key], COLOR, key, kind !== "deployment")
        : requestText(value[key], key === "login_message" ? 240 : key === "login_title" ? 120 : 80,
          key, kind !== "deployment");
    }
    return command;
  }
  if (action === "attach_asset") {
    exact(value, ["action", "asset_kind", "png_base64"], kind === "deployment" ? "workspace" : "platform", true);
    const encoded = String(value.png_base64 ?? "");
    if (!encoded || encoded.length > 700_000 || !/^[A-Za-z0-9+/]+={0,2}$/u.test(encoded)) {
      invalidRequest(kind === "deployment" ? "workspace" : "platform", "png_base64");
    }
    return { action, asset_kind: requestChoice(value.asset_kind, ["logo", "favicon"], "asset_kind", kind !== "deployment"), png_base64: encoded };
  }
  if (action === "remove_asset") {
    exact(value, ["action", "asset_kind"], kind === "deployment" ? "workspace" : "platform", true);
    return { action, asset_kind: requestChoice(value.asset_kind, ["logo", "favicon"], "asset_kind", kind !== "deployment") };
  }
  if (action === "request_domain") {
    exact(value, ["action", "domain"], "platform", true);
    return { action, domain: requestText(value.domain, 253, "domain", true).toLowerCase() };
  }
  exact(value, ["action"], kind === "deployment" ? "workspace" : "platform", true);
  return { action };
}

function brandInfo(value, kind, domain) {
  plain(value, "branding", domain);
  const commonKeys = [
    "status", "product_name", "primary_color", "secondary_color", "login_title",
    "login_message", "logo", "favicon", "revision", "source_sha256", "actions", "updated_at",
  ];
  const kindKeys = kind === "platform"
    ? ["platform_brand_id", "domain", "published_generation", "lkg_generation"]
    : kind === "channel"
      ? ["channel_brand_id", "domain", "contract_status", "contract_sha256", "contract_revision", "partner_scope_revision", "published_generation", "lkg_generation"]
      : ["deployment_brand_id", "desired_generation", "applied_generation", "lkg_generation", "ack_status"];
  exact(value, [...commonKeys, ...kindKeys], domain, false);
  const statuses = kind === "platform"
    ? ["draft", "ready", "released", "suspended"]
    : kind === "channel"
      ? ["draft", "pending_domain", "ready", "released", "suspended", "archived"]
      : ["draft", "ready", "published", "suspended"];
  const idKey = `${kind}_brand_id`;
  const actions = array(value.actions, 12, "actions", domain).map((item) => (
    choice(item, kind === "deployment"
      ? [...COMMON_ACTIONS, "publish"]
      : [...COMMON_ACTIONS, "request_domain", "release"], "action", domain)
  ));
  if (actions.length !== new Set(actions).size) invalidResponse(domain, "actions");
  const result = {
    [idKey]: pattern(value[idKey], IDS[kind], idKey, domain),
    status: choice(value.status, statuses, "status", domain),
    product_name: text(value.product_name, 80, "product_name", domain),
    primary_color: pattern(value.primary_color, COLOR, "primary_color", domain),
    secondary_color: pattern(value.secondary_color, COLOR, "secondary_color", domain),
    login_title: text(value.login_title, 120, "login_title", domain),
    login_message: text(value.login_message, 240, "login_message", domain),
    logo: value.logo === null ? null : asset(value.logo, domain),
    favicon: value.favicon === null ? null : asset(value.favicon, domain),
    revision: integer(value.revision, 1, "revision", domain),
    source_sha256: pattern(value.source_sha256, SHA256, "source_sha256", domain),
    actions: freeze(actions),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  };
  if (kind === "platform" || kind === "channel") {
    Object.assign(result, {
      domain: value.domain === null ? null : domainInfo(value.domain, domain),
      published_generation: integer(value.published_generation, 0, "published_generation", domain),
      lkg_generation: integer(value.lkg_generation, 0, "lkg_generation", domain),
    });
  }
  if (kind === "channel") Object.assign(result, {
    contract_status: choice(value.contract_status, ["active", "suspended", "retired"], "contract_status", domain),
    contract_sha256: pattern(value.contract_sha256, SHA256, "contract_sha256", domain),
    contract_revision: integer(value.contract_revision, 1, "contract_revision", domain),
    partner_scope_revision: value.partner_scope_revision === null ? null : integer(value.partner_scope_revision, 1, "partner_scope_revision", domain),
  });
  if (kind === "deployment") Object.assign(result, {
    desired_generation: integer(value.desired_generation, 0, "desired_generation", domain),
    applied_generation: integer(value.applied_generation, 0, "applied_generation", domain),
    lkg_generation: integer(value.lkg_generation, 0, "lkg_generation", domain),
    ack_status: value.ack_status === null ? null : choice(value.ack_status, ["pending", "applied", "rejected", "stale"], "ack_status", domain),
  });
  return freeze(result);
}

function asset(value, domain) {
  plain(value, "asset", domain);
  exact(value, ["asset_id", "kind", "status", "byte_count", "width", "height", "content_sha256"], domain, false);
  return freeze({
    asset_id: pattern(value.asset_id, IDS.asset, "asset_id", domain),
    kind: choice(value.kind, ["logo", "favicon"], "asset_kind", domain),
    status: choice(value.status, ["staged", "scanning", "clean", "rejected", "published", "quarantined"], "asset_status", domain),
    byte_count: integer(value.byte_count, 1, "byte_count", domain, 512 * 1024),
    width: integer(value.width, 16, "width", domain, 1024),
    height: integer(value.height, 16, "height", domain, 1024),
    content_sha256: pattern(value.content_sha256, SHA256, "content_sha256", domain),
  });
}

function domainInfo(value, domain) {
  plain(value, "domain", domain);
  exact(value, ["domain_id", "canonical_domain", "status", "observation_count", "certificate_ready", "routing_ready", "revision", "challenge"], domain, false);
  const challenge = value.challenge === null ? null : text(value.challenge, 256, "challenge", domain);
  return freeze({
    domain_id: pattern(value.domain_id, IDS.domain, "domain_id", domain),
    canonical_domain: text(value.canonical_domain, 253, "canonical_domain", domain),
    status: choice(value.status, ["pending", "verified", "active", "reverification_required", "revoked", "quarantined"], "domain_status", domain),
    observation_count: integer(value.observation_count, 0, "observation_count", domain, 2),
    certificate_ready: boolean(value.certificate_ready, "certificate_ready", domain),
    routing_ready: boolean(value.routing_ready, "routing_ready", domain),
    revision: integer(value.revision, 1, "domain_revision", domain),
    challenge,
  });
}

function publicBrand(value) {
  plain(value, "branding", "public");
  exact(value, ["product_name", "primary_color", "secondary_color", "login_title", "login_message", "logo_path", "favicon_path", "brand_generation", "public_etag"], "public", false);
  return freeze({
    product_name: text(value.product_name, 80, "product_name", "public"),
    primary_color: pattern(value.primary_color, COLOR, "primary_color", "public"),
    secondary_color: pattern(value.secondary_color, COLOR, "secondary_color", "public"),
    login_title: text(value.login_title, 120, "login_title", "public"),
    login_message: text(value.login_message, 240, "login_message", "public"),
    logo_path: value.logo_path === null ? null : pattern(value.logo_path, PUBLIC_ASSET, "logo_path", "public"),
    favicon_path: value.favicon_path === null ? null : pattern(value.favicon_path, PUBLIC_ASSET, "favicon_path", "public"),
    brand_generation: integer(value.brand_generation, 0, "brand_generation", "public"),
    public_etag: pattern(value.public_etag, /^"brand-[0-9a-f]{64}"$/u, "public_etag", "public"),
  });
}

function apiUrl(baseValue, path, domain) {
  let base;
  try { base = new URL(String(baseValue)); } catch { invalidRequest(domain, "control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest(domain, "control_api_url");
  }
  return new URL(path, base).toString();
}

function exact(value, keys, domain, request) {
  plain(value, "object", domain, request);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    if (request) invalidRequest(domain, "shape");
    invalidResponse(domain, "shape");
  }
}

function plain(value, label, domain, request = false) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) {
    if (request) invalidRequest(domain, label);
    invalidResponse(domain, label);
  }
}

function array(value, maximum, label, domain) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(domain, label);
  return value;
}

function requestPattern(value, expression, label, platform) {
  const textValue = String(value ?? "");
  if (!expression.test(textValue)) invalidRequest(platform ? "platform" : "workspace", label);
  return textValue;
}

function requestChoice(value, choices, label, platform) {
  const textValue = String(value ?? "");
  if (!choices.includes(textValue)) invalidRequest(platform ? "platform" : "workspace", label);
  return textValue;
}

function requestInteger(value, minimum, label, domain) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < minimum) invalidRequest(domain, label);
  return number;
}

function requestText(value, maximum, label, platform) {
  const textValue = String(value ?? "");
  if (!textValue || new TextEncoder().encode(textValue).length > maximum || hasControl(textValue)) {
    invalidRequest(platform ? "platform" : "workspace", label);
  }
  return textValue;
}

function text(value, maximum, label, domain) {
  const textValue = String(value ?? "");
  if (!textValue || new TextEncoder().encode(textValue).length > maximum || hasControl(textValue)) {
    invalidResponse(domain, label);
  }
  return textValue;
}

function pattern(value, expression, label, domain) {
  const textValue = String(value ?? "");
  if (!expression.test(textValue)) invalidResponse(domain, label);
  return textValue;
}

function choice(value, values, label, domain) {
  const textValue = String(value ?? "");
  if (!values.includes(textValue)) invalidResponse(domain, label);
  return textValue;
}

function integer(value, minimum, label, domain, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalidResponse(domain, label);
  return value;
}

function boolean(value, label, domain) {
  if (typeof value !== "boolean") invalidResponse(domain, label);
  return value;
}

function timestamp(value, label, domain) {
  const textValue = String(value ?? "");
  if (!Number.isFinite(Date.parse(textValue))) invalidResponse(domain, label);
  return textValue;
}

function trace(value, domain) {
  return {
    request_id: pattern(value.request_id, CHAIN, "request_id", domain),
    trace_id: pattern(value.trace_id, CHAIN, "trace_id", domain),
  };
}

function cacheDirectives(response) {
  return new Set(String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim()).filter(Boolean));
}

function safeText(value) {
  const textValue = String(value ?? "");
  return textValue.length <= 300 && !hasControl(textValue) ? textValue : "";
}

function safeChain(value) {
  return CHAIN.test(String(value ?? "")) ? String(value) : "";
}

function hasControl(value) {
  return /[\u0000-\u001f\u007f-\u009f]/u.test(value);
}

function invalidRequest(domain, label) {
  throw domainError(domain, 400, "branding.request_invalid", `Invalid branding ${label}`);
}

function invalidResponse(domain, label) {
  throw domainError(domain, 502, "branding.response_invalid", `Invalid branding ${label}`);
}

function domainError(domain, status, code, message, requestId = "") {
  const ErrorType = domain === "platform" ? PlatformApiError : AuthApiError;
  return new ErrorType({ status, code, message, requestId });
}

function freeze(value) { return Object.freeze(value); }
