import { IdentityApiError } from "./identity.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

export const NOTIFICATION_EVENT_PARAMETERS = Object.freeze({
  "security.sign_in_risk": Object.freeze(["event_label", "occurred_at"]),
  "subscription.status_changed": Object.freeze(["effective_at", "plan_name", "status"]),
  "verification.status_changed": Object.freeze(["status", "updated_at"]),
  "payment.status_changed": Object.freeze(["amount", "currency", "status", "updated_at"]),
  "invoice.issued": Object.freeze([
    "currency",
    "invoice_number_masked",
    "issued_at",
    "total",
  ]),
  "invoice.reversed": Object.freeze(["invoice_number_masked", "reversed_at"]),
  "infrastructure.backup_completed": Object.freeze(["completed_at", "status"]),
  "gateway.health_changed": Object.freeze(["cell_label", "observed_at", "status"]),
});

const CATEGORIES = new Set([
  "security",
  "billing",
  "verification",
  "payments",
  "invoices",
  "operations",
]);
const CHANNELS = new Set(["in_app", "email", "webhook", "sms", "web_push"]);
const TEMPLATE_CHANNELS = new Set(["in_app", "email", "webhook"]);
const TEMPLATE_STATUSES = new Set(["draft", "published"]);
const NOTIFICATION_ID = /^ntf_[0-9a-f]{32}$/u;
const TEMPLATE_ID = /^ntp_[0-9a-f]{32}$/u;
const TEMPLATE_CODE = /^[a-z0-9][a-z0-9_.-]{2,79}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const CONTENT_HASH = /^[0-9a-f]{64}$/u;

export function listWorkspaceNotifications({
  controlApiUrl,
  unreadOnly = false,
  limit = 30,
  cursor = null,
  signal,
} = {}) {
  if (typeof unreadOnly !== "boolean") workspaceClientError("unread_only");
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) workspaceClientError("limit");
  const query = new URLSearchParams({
    unread_only: String(unreadOnly),
    limit: String(limit),
  });
  if (cursor !== null) query.set("cursor", notificationId(cursor, workspaceClientError));
  return workspaceRequest(controlApiUrl, `/notifications?${query}`, {
    response: validateNotificationList,
    signal,
  });
}

export function markWorkspaceNotificationRead({ controlApiUrl, notificationId: id, signal } = {}) {
  return workspaceRequest(
    controlApiUrl,
    `/notifications/${encodeURIComponent(notificationId(id, workspaceClientError))}/read`,
    {
      method: "PATCH",
      body: {},
      csrf: true,
      response: validateNotificationRead,
      signal,
    },
  );
}

export function getWorkspaceNotificationPreferences({ controlApiUrl, signal } = {}) {
  return workspaceRequest(controlApiUrl, "/notification-preferences", {
    response: validatePreferences,
    signal,
  });
}

export function updateWorkspaceNotificationPreferences({
  controlApiUrl,
  revision,
  changes,
  signal,
} = {}) {
  if (!Number.isSafeInteger(revision) || revision < 1) workspaceClientError("revision");
  if (!Array.isArray(changes) || changes.length < 1 || changes.length > 30) {
    workspaceClientError("changes");
  }
  const seen = new Set();
  const body = changes.map((change) => {
    object(change, "change", workspaceClientError);
    exact(change, ["category", "channel", "enabled"], "change", workspaceClientError);
    const category = choice(change.category, CATEGORIES, "category", workspaceClientError);
    const channel = choice(change.channel, CHANNELS, "channel", workspaceClientError);
    if (typeof change.enabled !== "boolean") workspaceClientError("enabled");
    const key = `${category}:${channel}`;
    if (seen.has(key)) workspaceClientError("changes");
    seen.add(key);
    return { category, channel, enabled: change.enabled };
  });
  return workspaceRequest(controlApiUrl, "/notification-preferences", {
    method: "PATCH",
    body: { changes: body },
    csrf: true,
    headers: { "If-Match": `"${revision}"` },
    response: validatePreferences,
    signal,
  });
}

export function listPlatformNotificationTemplates({ controlApiUrl, signal } = {}) {
  return platformRequest(controlApiUrl, "/platform/notification-templates", {
    response: validateTemplateList,
    signal,
  });
}

export function createPlatformNotificationTemplate({
  controlApiUrl,
  revision,
  idempotencyKey,
  template,
  signal,
} = {}) {
  if (!Number.isSafeInteger(revision) || revision < 1) platformClientError("revision");
  const key = String(idempotencyKey ?? "");
  if (!IDEMPOTENCY_KEY.test(key)) platformClientError("idempotency_key");
  const body = templatePayload(template);
  return platformRequest(controlApiUrl, "/platform/notification-templates", {
    method: "POST",
    body,
    csrf: true,
    headers: { "If-Match": `"${revision}"`, "Idempotency-Key": key },
    response: validateTemplateResponse,
    signal,
  });
}

async function workspaceRequest(controlApiUrl, path, options) {
  return notificationRequest(controlApiUrl, path, options, {
    ErrorType: IdentityApiError,
    csrf: readWorkspaceCsrfCookie,
    networkCode: "notification.network_unavailable",
    networkMessage: "暂时无法连接通知服务。",
    responseCode: "notification.response_invalid",
  });
}

async function platformRequest(controlApiUrl, path, options) {
  return notificationRequest(controlApiUrl, path, options, {
    ErrorType: PlatformApiError,
    csrf: readPlatformCsrfCookie,
    networkCode: "notification.network_unavailable",
    networkMessage: "暂时无法连接模板服务。",
    responseCode: "notification.response_invalid",
  });
}

async function notificationRequest(controlApiUrl, path, options, context) {
  const headers = { Accept: "application/json", ...(options.headers ?? {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = context.csrf();
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, context.ErrorType), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new context.ErrorType({ code: context.networkCode, message: context.networkMessage });
  }
  const payload = await json(response, context);
  if (!response.ok) {
    throw new context.ErrorType({
      status: Number(response.status) || 0,
      code: bounded(payload?.code, 120) || "notification.request_failed",
      message: bounded(payload?.message, 500) || "通知请求未完成。",
      requestId: bounded(payload?.request_id, 160) || "-",
    });
  }
  try {
    return options.response(payload);
  } catch {
    throw new context.ErrorType({
      code: context.responseCode,
      message: "通知服务响应无效。",
    });
  }
}

function templatePayload(value) {
  object(value, "template", platformClientError);
  exact(
    value,
    [
      "template_code",
      "event_type",
      "channel",
      "locale",
      "parameter_allowlist",
      "subject_template",
      "body_template",
      "status",
    ],
    "template",
    platformClientError,
  );
  const templateCode = String(value.template_code ?? "").trim();
  const eventType = choice(
    value.event_type,
    new Set(Object.keys(NOTIFICATION_EVENT_PARAMETERS)),
    "event_type",
    platformClientError,
  );
  const channel = choice(value.channel, TEMPLATE_CHANNELS, "channel", platformClientError);
  const locale = choice(value.locale, new Set(["en", "zh-CN"]), "locale", platformClientError);
  const status = choice(value.status, TEMPLATE_STATUSES, "status", platformClientError);
  if (!TEMPLATE_CODE.test(templateCode)) platformClientError("template_code");
  const expected = NOTIFICATION_EVENT_PARAMETERS[eventType];
  if (
    !Array.isArray(value.parameter_allowlist)
    || value.parameter_allowlist.length !== expected.length
    || value.parameter_allowlist.some((item, index) => item !== expected[index])
  ) {
    platformClientError("parameter_allowlist");
  }
  const subject = templateText(value.subject_template, 200, "subject_template");
  const body = templateText(value.body_template, 4000, "body_template");
  const combined = `${subject}\n${body}`;
  const tokens = [...combined.matchAll(/\{\{([a-z][a-z0-9_]*)\}\}/gu)].map((match) => match[1]);
  const residue = combined.replace(/\{\{[a-z][a-z0-9_]*\}\}/gu, "");
  if (
    /[{}<>]|\{%|\{#/u.test(residue)
    || new Set(tokens).size !== expected.length
    || expected.some((parameter) => !tokens.includes(parameter))
  ) {
    platformClientError("template");
  }
  return {
    template_code: templateCode,
    event_type: eventType,
    channel,
    locale,
    parameter_allowlist: [...expected],
    subject_template: subject,
    body_template: body,
    status,
  };
}

function validateNotificationList(payload) {
  object(payload, "response", workspaceClientError);
  exact(
    payload,
    ["notifications", "unread_count", "next_cursor", "request_id", "trace_id"],
    "response",
    workspaceClientError,
  );
  if (!Array.isArray(payload.notifications) || payload.notifications.length > 100) {
    workspaceClientError("notifications");
  }
  return {
    notifications: payload.notifications.map(validateNotification),
    unread_count: nonnegative(payload.unread_count, "unread_count", workspaceClientError),
    next_cursor: payload.next_cursor === null
      ? null
      : notificationId(payload.next_cursor, workspaceClientError),
    request_id: text(payload.request_id, 160, "request_id", workspaceClientError),
    trace_id: text(payload.trace_id, 160, "trace_id", workspaceClientError),
  };
}

function validateNotificationRead(payload) {
  object(payload, "response", workspaceClientError);
  exact(payload, ["notification", "request_id", "trace_id"], "response", workspaceClientError);
  return {
    notification: validateNotification(payload.notification),
    request_id: text(payload.request_id, 160, "request_id", workspaceClientError),
    trace_id: text(payload.trace_id, 160, "trace_id", workspaceClientError),
  };
}

function validateNotification(value) {
  object(value, "notification", workspaceClientError);
  exact(
    value,
    ["notification_id", "event_type", "category", "subject", "body", "read", "created_at"],
    "notification",
    workspaceClientError,
  );
  if (typeof value.read !== "boolean") workspaceClientError("read");
  return {
    notification_id: notificationId(value.notification_id, workspaceClientError),
    event_type: choice(
      value.event_type,
      new Set(Object.keys(NOTIFICATION_EVENT_PARAMETERS)),
      "event_type",
      workspaceClientError,
    ),
    category: choice(value.category, CATEGORIES, "category", workspaceClientError),
    subject: text(value.subject, 200, "subject", workspaceClientError),
    body: text(value.body, 4000, "body", workspaceClientError),
    read: value.read,
    created_at: timestamp(value.created_at, "created_at", workspaceClientError),
  };
}

function validatePreferences(payload) {
  object(payload, "response", workspaceClientError);
  exact(payload, ["preferences", "revision", "request_id", "trace_id"], "response", workspaceClientError);
  if (!Array.isArray(payload.preferences) || payload.preferences.length > 30) {
    workspaceClientError("preferences");
  }
  return {
    preferences: payload.preferences.map((value) => {
      object(value, "preference", workspaceClientError);
      exact(
        value,
        ["category", "channel", "enabled", "mandatory", "available"],
        "preference",
        workspaceClientError,
      );
      if (
        typeof value.enabled !== "boolean"
        || typeof value.mandatory !== "boolean"
        || typeof value.available !== "boolean"
      ) {
        workspaceClientError("preference");
      }
      return {
        category: choice(value.category, CATEGORIES, "category", workspaceClientError),
        channel: choice(value.channel, CHANNELS, "channel", workspaceClientError),
        enabled: value.enabled,
        mandatory: value.mandatory,
        available: value.available,
      };
    }),
    revision: positive(payload.revision, "revision", workspaceClientError),
    request_id: text(payload.request_id, 160, "request_id", workspaceClientError),
    trace_id: text(payload.trace_id, 160, "trace_id", workspaceClientError),
  };
}

function validateTemplateList(payload) {
  object(payload, "response", platformClientError);
  exact(
    payload,
    ["templates", "catalog_revision", "request_id", "trace_id"],
    "response",
    platformClientError,
  );
  if (!Array.isArray(payload.templates) || payload.templates.length > 200) {
    platformClientError("templates");
  }
  return {
    templates: payload.templates.map(validateTemplate),
    catalog_revision: positive(payload.catalog_revision, "catalog_revision", platformClientError),
    request_id: text(payload.request_id, 160, "request_id", platformClientError),
    trace_id: text(payload.trace_id, 160, "trace_id", platformClientError),
  };
}

function validateTemplateResponse(payload) {
  object(payload, "response", platformClientError);
  exact(
    payload,
    ["template", "catalog_revision", "idempotent_replay", "request_id", "trace_id"],
    "response",
    platformClientError,
  );
  if (typeof payload.idempotent_replay !== "boolean") platformClientError("idempotent_replay");
  return {
    template: validateTemplate(payload.template),
    catalog_revision: positive(payload.catalog_revision, "catalog_revision", platformClientError),
    idempotent_replay: payload.idempotent_replay,
    request_id: text(payload.request_id, 160, "request_id", platformClientError),
    trace_id: text(payload.trace_id, 160, "trace_id", platformClientError),
  };
}

function validateTemplate(value) {
  object(value, "template", platformClientError);
  exact(
    value,
    [
      "notification_template_id",
      "template_code",
      "event_type",
      "category",
      "channel",
      "locale",
      "version",
      "parameter_allowlist",
      "subject_template",
      "body_template",
      "content_sha256",
      "status",
      "revision",
      "created_at",
      "published_at",
    ],
    "template",
    platformClientError,
  );
  const eventType = choice(
    value.event_type,
    new Set(Object.keys(NOTIFICATION_EVENT_PARAMETERS)),
    "event_type",
    platformClientError,
  );
  const allowlist = value.parameter_allowlist;
  const expected = NOTIFICATION_EVENT_PARAMETERS[eventType];
  if (
    !Array.isArray(allowlist)
    || allowlist.length !== expected.length
    || allowlist.some((item, index) => item !== expected[index])
  ) {
    platformClientError("parameter_allowlist");
  }
  const hash = String(value.content_sha256 ?? "");
  if (!TEMPLATE_ID.test(String(value.notification_template_id ?? "")) || !CONTENT_HASH.test(hash)) {
    platformClientError("template");
  }
  const code = String(value.template_code ?? "");
  if (!TEMPLATE_CODE.test(code)) platformClientError("template_code");
  return {
    notification_template_id: value.notification_template_id,
    template_code: code,
    event_type: eventType,
    category: choice(value.category, CATEGORIES, "category", platformClientError),
    channel: choice(value.channel, TEMPLATE_CHANNELS, "channel", platformClientError),
    locale: choice(value.locale, new Set(["en", "zh-CN"]), "locale", platformClientError),
    version: positive(value.version, "version", platformClientError),
    parameter_allowlist: [...allowlist],
    subject_template: text(value.subject_template, 200, "subject_template", platformClientError),
    body_template: text(value.body_template, 4000, "body_template", platformClientError),
    content_sha256: hash,
    status: choice(value.status, TEMPLATE_STATUSES, "status", platformClientError),
    revision: positive(value.revision, "revision", platformClientError),
    created_at: timestamp(value.created_at, "created_at", platformClientError),
    published_at: value.published_at === null
      ? null
      : timestamp(value.published_at, "published_at", platformClientError),
  };
}

async function json(response, context) {
  let raw;
  try {
    raw = await response.text();
    if (!raw || raw.length > 64 * 1024) throw new Error("invalid");
    return JSON.parse(raw);
  } catch {
    throw new context.ErrorType({ code: context.responseCode, message: "通知服务响应无效。" });
  }
}

function apiUrl(controlApiUrl, path, ErrorType) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    throw new ErrorType({ code: "notification.configuration_invalid", message: "通知服务地址无效。" });
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw new ErrorType({ code: "notification.configuration_invalid", message: "通知服务地址无效。" });
  }
  return new URL(path, base).toString();
}

function object(value, label, invalid) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) invalid(label);
}

function exact(value, keys, label, invalid) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    invalid(label);
  }
}

function choice(value, choices, label, invalid) {
  if (typeof value !== "string" || !choices.has(value)) invalid(label);
  return value;
}

function text(value, maximum, label, invalid) {
  if (typeof value !== "string" || !value || value.length > maximum || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) {
    invalid(label);
  }
  return value;
}

function templateText(value, maximum, label) {
  return text(value, maximum, label, platformClientError);
}

function timestamp(value, label, invalid) {
  const result = text(value, 40, label, invalid);
  if (!Number.isFinite(Date.parse(result))) invalid(label);
  return result;
}

function positive(value, label, invalid) {
  if (!Number.isSafeInteger(value) || value < 1) invalid(label);
  return value;
}

function nonnegative(value, label, invalid) {
  if (!Number.isSafeInteger(value) || value < 0) invalid(label);
  return value;
}

function notificationId(value, invalid) {
  const result = String(value ?? "");
  if (!NOTIFICATION_ID.test(result)) invalid("notification_id");
  return result;
}

function bounded(value, maximum) {
  return typeof value === "string" && value.length <= maximum ? value : "";
}

function workspaceClientError(label) {
  throw new IdentityApiError({
    code: "notification.request_invalid",
    message: `通知请求字段无效：${label}。`,
  });
}

function platformClientError(label) {
  throw new PlatformApiError({
    code: "notification.request_invalid",
    message: `模板请求字段无效：${label}。`,
  });
}
