import { IdentityApiError } from "./identity.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const STATEMENT_ID = /^bst_[0-9a-f]{32}$/u;
const INVOICE_ID = /^ivr_[0-9a-f]{32}$/u;
const ARTIFACT_ID = /^ina_[0-9a-f]{32}$/u;
const REVERSAL_ID = /^irr_[0-9a-f]{32}$/u;
const RECONCILIATION_ID = /^rec_[0-9a-f]{32}$/u;
const DIFFERENCE_ID = /^rcd_[0-9a-f]{32}$/u;
const SOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const CURSOR = /^[A-Za-z0-9_-]{1,512}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const DOWNLOAD_TOKEN = /^[A-Za-z0-9_-]{32,128}$/u;
const CURRENCIES = new Set(["CNY", "USD"]);
const INVOICE_TYPES = new Set(["electronic_general", "electronic_special"]);
const INVOICE_STATUSES = new Set([
  "requested", "processing", "issued", "failed", "reversal_pending", "reversed",
]);
const ARTIFACT_KINDS = new Set(["invoice_pdf", "invoice_csv", "red_letter_pdf"]);
const MIME_TYPES = new Set(["application/pdf", "text/csv"]);
const REVERSAL_REASONS = new Set(["invoice_error", "customer_correction", "contract_canceled"]);
const APPROVAL_REASONS = new Set([
  "facts_confirmed", "policy_confirmed", "evidence_missing", "policy_blocked",
]);
const DIFFERENCE_TYPES = new Set([
  "order_payment_amount_mismatch",
  "paid_order_missing_capture",
  "closed_cycle_missing_statement",
  "meter_summary_watermark_mismatch",
  "issued_invoice_missing_artifact",
]);
const LEFT_SOURCE_TYPES = new Set(["order", "billing_cycle", "meter_summary", "invoice_request"]);
const RIGHT_SOURCE_TYPES = new Set([
  "payment_intent", "billing_statement", "meter_reading", "invoice_artifact",
]);
const MAX_ARTIFACT_BYTES = 20_971_520;

export function listWorkspaceBillingStatements({ controlApiUrl, signal } = {}) {
  return jsonRequest(controlApiUrl, "/billing/statements", { signal }, "workspace");
}

export function createWorkspaceInvoice({
  controlApiUrl,
  billingStatementId,
  invoiceType,
  idempotencyKey,
  signal,
} = {}) {
  const payload = {
    billing_statement_id: checkedId(STATEMENT_ID, billingStatementId, "workspace"),
    invoice_type: checkedEnum(INVOICE_TYPES, invoiceType, "workspace"),
  };
  return jsonRequest(controlApiUrl, "/invoices", {
    method: "POST",
    headers: workspaceWriteHeaders(idempotencyKey),
    body: JSON.stringify(payload),
    signal,
  }, "workspace");
}

export function getWorkspaceInvoice({ controlApiUrl, invoiceId, signal } = {}) {
  const id = checkedId(INVOICE_ID, invoiceId, "workspace");
  return jsonRequest(controlApiUrl, `/invoices/${id}`, { signal }, "workspace");
}

export async function downloadWorkspaceInvoiceArtifact({
  controlApiUrl,
  invoiceId,
  artifact,
  signal,
} = {}) {
  const id = checkedId(INVOICE_ID, invoiceId, "workspace");
  if (!validArtifact(artifact)) invalidRequest("workspace");
  const artifactId = artifact.invoice_artifact_id;
  const grant = await jsonRequest(
    controlApiUrl,
    `/invoices/${id}/artifacts/${artifactId}/download-token`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": readWorkspaceCsrfCookie(),
      },
      body: "{}",
      signal,
    },
    "workspace",
  );
  let token = grant.download_token;
  try {
    const response = await rawRequest(controlApiUrl, "/invoice-downloads", {
      headers: { "X-MetaHub-Invoice-Download-Token": token },
      signal,
    }, "workspace", "*/*");
    if (!response.ok) throw responseErrorFromPayload(response, await readJson(response), "workspace");
    const filename = checkedDownloadHeaders(response, artifact);
    const blob = await response.blob();
    if (!(blob instanceof Blob) || blob.size !== artifact.size_bytes) responseInvalid("workspace");
    const digest = await sha256Hex(await blob.arrayBuffer());
    if (digest !== artifact.content_sha256) responseInvalid("workspace");
    return { blob, filename };
  } finally {
    token = "";
    grant.download_token = "";
  }
}

export function saveWorkspaceInvoiceArtifact(result, environment = globalThis) {
  const documentApi = environment.document;
  const urlApi = environment.URL;
  if (!(result?.blob instanceof Blob) || !validFilename(result.filename)
    || typeof urlApi?.createObjectURL !== "function"
    || typeof urlApi?.revokeObjectURL !== "function"
    || typeof documentApi?.createElement !== "function" || !documentApi.body) {
    responseInvalid("workspace");
  }
  const objectUrl = urlApi.createObjectURL(result.blob);
  const link = documentApi.createElement("a");
  try {
    link.href = objectUrl;
    link.download = result.filename;
    link.rel = "noopener";
    link.hidden = true;
    documentApi.body.append(link);
    link.click();
  } finally {
    link.remove();
    urlApi.revokeObjectURL(objectUrl);
  }
}

export function createPlatformReconciliation({ controlApiUrl, confirmed, signal } = {}) {
  if (confirmed !== true) invalidRequest("platform");
  return jsonRequest(controlApiUrl, "/platform/reconciliations", {
    method: "POST",
    headers: platformWriteHeaders(),
    body: JSON.stringify({ scope: "all", confirmed: true }),
    signal,
  }, "platform");
}

export function listPlatformReconciliationDifferences({
  controlApiUrl,
  reconciliationId,
  cursor,
  limit = 50,
  signal,
} = {}) {
  const id = checkedId(RECONCILIATION_ID, reconciliationId, "platform");
  const parameters = new URLSearchParams({ limit: String(boundedInteger(limit, 1, 100, "platform")) });
  if (cursor !== undefined && cursor !== null) {
    const value = String(cursor);
    if (!CURSOR.test(value)) invalidRequest("platform");
    parameters.set("cursor", value);
  }
  return jsonRequest(
    controlApiUrl,
    `/platform/reconciliations/${id}/differences?${parameters.toString()}`,
    { signal },
    "platform",
  );
}

export function createPlatformInvoiceReversal({
  controlApiUrl,
  invoiceId,
  reasonCode,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("platform");
  const id = checkedId(INVOICE_ID, invoiceId, "platform");
  const payload = {
    reason_code: checkedEnum(REVERSAL_REASONS, reasonCode, "platform"),
    confirmed: true,
  };
  return jsonRequest(controlApiUrl, `/platform/invoices/${id}/reversals`, {
    method: "POST",
    headers: platformWriteHeaders(idempotencyKey),
    body: JSON.stringify(payload),
    signal,
  }, "platform");
}

export function reviewPlatformInvoiceReversal({
  controlApiUrl,
  reversalId,
  decision,
  reasonCode,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("platform");
  const id = checkedId(REVERSAL_ID, reversalId, "platform");
  const normalizedDecision = checkedEnum(new Set(["approve", "reject"]), decision, "platform");
  const normalizedReason = checkedEnum(APPROVAL_REASONS, reasonCode, "platform");
  return jsonRequest(controlApiUrl, `/platform/invoice-reversals/${id}/approvals`, {
    method: "POST",
    headers: platformWriteHeaders(idempotencyKey),
    body: JSON.stringify({
      decision: normalizedDecision,
      reason_code: normalizedReason,
      confirmed: true,
    }),
    signal,
  }, "platform");
}

async function jsonRequest(controlApiUrl, path, options, domain) {
  const response = await rawRequest(controlApiUrl, path, options, domain);
  const payload = await readJson(response);
  if (!response.ok) throw responseErrorFromPayload(response, payload, domain);
  if (!validResponse(payload, path)) responseInvalid(domain);
  return payload;
}

async function rawRequest(controlApiUrl, path, options, domain, accept = "application/json") {
  try {
    return await fetch(apiUrl(controlApiUrl, path, domain), {
      method: options?.method ?? "GET",
      headers: { Accept: accept, ...(options?.headers ?? {}) },
      body: options?.body,
      credentials: "include",
      cache: "no-store",
      signal: options?.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError(domain, "invoice.network_unavailable", "暂时无法连接发票与对账服务。");
  }
}

function validResponse(payload, path) {
  if (!plainObject(payload) || !linkId(payload.request_id) || !linkId(payload.trace_id)) return false;
  if (path === "/billing/statements") {
    return exactKeys(payload, ["statements", "request_id", "trace_id"])
      && boundedArray(payload.statements, validStatement, 100);
  }
  if (path === "/invoices" || /^\/invoices\/ivr_[0-9a-f]{32}$/u.test(path)) {
    return exactKeys(payload, ["invoice", "idempotent_replay", "request_id", "trace_id"])
      && typeof payload.idempotent_replay === "boolean" && validInvoice(payload.invoice);
  }
  if (path.endsWith("/download-token")) {
    return exactKeys(payload, [
      "invoice_request_id", "invoice_artifact_id", "download_token", "expires_at",
      "request_id", "trace_id",
    ]) && INVOICE_ID.test(String(payload.invoice_request_id))
      && ARTIFACT_ID.test(String(payload.invoice_artifact_id))
      && DOWNLOAD_TOKEN.test(String(payload.download_token)) && timestamp(payload.expires_at);
  }
  if (path === "/platform/reconciliations") {
    return exactKeys(payload, ["reconciliation", "request_id", "trace_id"])
      && validReconciliation(payload.reconciliation);
  }
  if (path.includes("/differences?")) {
    return exactKeys(payload, [
      "reconciliation", "differences", "next_cursor", "request_id", "trace_id",
    ]) && validReconciliation(payload.reconciliation)
      && boundedArray(payload.differences, validDifference, 100)
      && (payload.next_cursor === null || CURSOR.test(String(payload.next_cursor)));
  }
  if (path.includes("/reversals") || path.includes("invoice-reversals")) {
    return exactKeys(payload, ["reversal", "idempotent_replay", "request_id", "trace_id"])
      && typeof payload.idempotent_replay === "boolean" && validReversal(payload.reversal);
  }
  return false;
}

function validStatement(value) {
  return plainObject(value) && exactKeys(value, [
    "billing_statement_id", "billing_cycle_id", "statement_kind", "immutable_version",
    "currency", "subtotal_minor", "tax_rate_basis_points", "tax_minor", "total_minor",
    "status", "issued_at",
  ]) && STATEMENT_ID.test(String(value.billing_statement_id))
    && /^pbc_[0-9a-f]{32}$/u.test(String(value.billing_cycle_id))
    && new Set(["standard", "credit", "replacement"]).has(value.statement_kind)
    && positiveInteger(value.immutable_version) && CURRENCIES.has(value.currency)
    && moneyInteger(value.subtotal_minor) && boundedIntegerValue(value.tax_rate_basis_points, 0, 10_000)
    && moneyInteger(value.tax_minor) && moneyInteger(value.total_minor)
    && value.total_minor === value.subtotal_minor + value.tax_minor
    && value.status === "published" && timestamp(value.issued_at);
}

function validInvoice(value) {
  return plainObject(value) && exactKeys(value, [
    "invoice_request_id", "billing_statement_id", "invoice_type", "status", "currency",
    "total_minor", "billing_profile_revision", "state_revision", "artifacts", "requested_at",
    "updated_at",
  ]) && INVOICE_ID.test(String(value.invoice_request_id))
    && STATEMENT_ID.test(String(value.billing_statement_id)) && INVOICE_TYPES.has(value.invoice_type)
    && INVOICE_STATUSES.has(value.status) && CURRENCIES.has(value.currency)
    && moneyInteger(value.total_minor) && positiveInteger(value.billing_profile_revision)
    && positiveInteger(value.state_revision)
    && boundedArray(value.artifacts, (artifact) => validArtifact(artifact, value.invoice_request_id), 20)
    && timestamp(value.requested_at) && timestamp(value.updated_at);
}

function validArtifact(value, expectedInvoiceId = null) {
  return plainObject(value) && exactKeys(value, [
    "invoice_artifact_id", "artifact_kind", "invoice_number_masked", "mime_type", "size_bytes",
    "content_sha256", "issued_at", "download_action",
  ]) && ARTIFACT_ID.test(String(value.invoice_artifact_id)) && ARTIFACT_KINDS.has(value.artifact_kind)
    && typeof value.invoice_number_masked === "string" && value.invoice_number_masked.length >= 4
    && value.invoice_number_masked.length <= 32 && MIME_TYPES.has(value.mime_type)
    && boundedIntegerValue(value.size_bytes, 1, MAX_ARTIFACT_BYTES)
    && HASH.test(String(value.content_sha256)) && timestamp(value.issued_at)
    && /^\/invoices\/ivr_[0-9a-f]{32}\/artifacts\/ina_[0-9a-f]{32}\/download-token$/u.test(value.download_action)
    && (expectedInvoiceId === null
      || value.download_action === `/invoices/${expectedInvoiceId}/artifacts/${value.invoice_artifact_id}/download-token`);
}

function validReconciliation(value) {
  return plainObject(value) && exactKeys(value, [
    "reconciliation_run_id", "scope", "order_watermark", "payment_watermark", "meter_watermark",
    "statement_watermark", "invoice_watermark", "status", "generation", "created_at", "completed_at",
  ]) && RECONCILIATION_ID.test(String(value.reconciliation_run_id)) && value.scope === "all"
    && ["order_watermark", "payment_watermark", "meter_watermark", "statement_watermark", "invoice_watermark"]
      .every((key) => nonnegativeInteger(value[key]))
    && new Set(["queued", "processing", "completed", "failed"]).has(value.status)
    && positiveInteger(value.generation) && timestamp(value.created_at)
    && (value.completed_at === null || timestamp(value.completed_at));
}

function validDifference(value) {
  return plainObject(value) && exactKeys(value, [
    "reconciliation_difference_id", "difference_type", "left_source_type", "left_source_id",
    "right_source_type", "right_source_id", "currency", "amount_difference_minor", "status",
    "adjustment_reference", "detected_at",
  ]) && DIFFERENCE_ID.test(String(value.reconciliation_difference_id))
    && DIFFERENCE_TYPES.has(value.difference_type) && LEFT_SOURCE_TYPES.has(value.left_source_type)
    && SOURCE_ID.test(String(value.left_source_id))
    && ((value.right_source_type === null && value.right_source_id === null)
      || (RIGHT_SOURCE_TYPES.has(value.right_source_type) && SOURCE_ID.test(String(value.right_source_id))))
    && CURRENCIES.has(value.currency) && moneyInteger(value.amount_difference_minor)
    && new Set(["open", "acknowledged", "adjusted"]).has(value.status)
    && (value.adjustment_reference === null || SOURCE_ID.test(String(value.adjustment_reference)))
    && timestamp(value.detected_at);
}

function validReversal(value) {
  return plainObject(value) && exactKeys(value, [
    "invoice_reversal_request_id", "invoice_request_id", "reason_code", "status",
    "approved_domains", "state_revision", "requested_at", "updated_at",
  ]) && REVERSAL_ID.test(String(value.invoice_reversal_request_id))
    && INVOICE_ID.test(String(value.invoice_request_id)) && REVERSAL_REASONS.has(value.reason_code)
    && new Set(["pending_approvals", "queued", "completed", "rejected"]).has(value.status)
    && boundedArray(value.approved_domains, (item) => new Set(["billing", "compliance"]).has(item), 2)
    && new Set(value.approved_domains).size === value.approved_domains.length
    && positiveInteger(value.state_revision) && timestamp(value.requested_at)
    && timestamp(value.updated_at);
}

function checkedDownloadHeaders(response, artifact) {
  const type = String(response.headers.get("Content-Type") ?? "").split(";", 1)[0].trim().toLowerCase();
  const disposition = String(response.headers.get("Content-Disposition") ?? "");
  const cacheControl = String(response.headers.get("Cache-Control") ?? "").toLowerCase();
  const nosniff = String(response.headers.get("X-Content-Type-Options") ?? "").toLowerCase();
  const filename = disposition.match(/^attachment;\s*filename="([A-Za-z0-9._-]+\.(?:pdf|csv))"$/u)?.[1] ?? "";
  if (type !== artifact.mime_type || !validFilename(filename)
    || !cacheControl.includes("no-store") || !cacheControl.includes("private")
    || nosniff !== "nosniff") responseInvalid("workspace");
  return filename;
}

function workspaceWriteHeaders(idempotencyKey) {
  return {
    "Content-Type": "application/json",
    "X-CSRF-Token": readWorkspaceCsrfCookie(),
    "Idempotency-Key": checkedId(IDEMPOTENCY_KEY, idempotencyKey, "workspace"),
  };
}

function platformWriteHeaders(idempotencyKey = null) {
  const headers = {
    "Content-Type": "application/json",
    "X-CSRF-Token": readPlatformCsrfCookie(),
  };
  if (idempotencyKey !== null) {
    headers["Idempotency-Key"] = checkedId(IDEMPOTENCY_KEY, idempotencyKey, "platform");
  }
  return headers;
}

function apiUrl(controlApiUrl, path, domain) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidRequest(domain);
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest(domain);
  }
  return new URL(path, base).toString();
}

function checkedId(regex, value, domain) {
  const text = String(value ?? "");
  if (!regex.test(text)) invalidRequest(domain);
  return text;
}

function checkedEnum(values, value, domain) {
  const text = String(value ?? "");
  if (!values.has(text)) invalidRequest(domain);
  return text;
}

function clientError(domain, code, message, status = 0, requestId = "-") {
  const ErrorType = domain === "platform" ? PlatformApiError : IdentityApiError;
  return new ErrorType({ status, code, message, requestId });
}

function invalidRequest(domain) {
  throw clientError(domain, "invoice.request_invalid", "发票与对账请求字段无效。");
}

function responseInvalid(domain) {
  throw clientError(domain, "invoice.response_invalid", "发票与对账响应格式无效。", 502);
}

function responseErrorFromPayload(response, payload, domain) {
  return clientError(
    domain,
    typeof payload?.code === "string" ? payload.code : "invoice.request_failed",
    typeof payload?.message === "string" ? payload.message : "发票与对账请求未完成，请稍后重试。",
    response.status,
    linkId(payload?.request_id) ? payload.request_id : "-",
  );
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function sha256Hex(buffer) {
  const cryptoApi = globalThis.crypto?.subtle;
  if (!cryptoApi) responseInvalid("workspace");
  const digest = await cryptoApi.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function validFilename(value) {
  return /^invoice-[0-9a-f]{8}-[0-9a-f]{8}\.(?:pdf|csv)$/u.test(String(value ?? ""));
}

function plainObject(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function boundedArray(value, validator, maximum) {
  return Array.isArray(value) && value.length <= maximum && value.every(validator);
}

function boundedInteger(value, minimum, maximum, domain) {
  const number = Number(value);
  if (!boundedIntegerValue(number, minimum, maximum)) invalidRequest(domain);
  return number;
}

function boundedIntegerValue(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value >= 1;
}

function nonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function moneyInteger(value) {
  return Number.isSafeInteger(value) && Math.abs(value) <= 1_000_000_000_000;
}

function timestamp(value) {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value));
}

function linkId(value) {
  return LINK_ID.test(String(value ?? ""));
}
