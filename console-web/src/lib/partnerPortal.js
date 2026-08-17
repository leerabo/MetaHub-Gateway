// PRD-087 伙伴门户客户端：负责租户隔离的请求、响应校验与安全附件下载边界。
import { AuthApiError } from "./auth.js";

const PARTNER_KEYS = Object.freeze([
  "partner_id", "partner_code", "display_name", "portal_name", "brand_primary_color",
  "support_text", "status", "default_commission_rate_bps", "revision", "created_at", "updated_at",
]);
const PROFILE_KEYS = Object.freeze(["partner", "request_id", "trace_id"]);
const PROJECT_LIST_KEYS = Object.freeze(["items", "total", "request_id", "trace_id"]);
const PROJECT_KEYS = Object.freeze(["project_id", "project_code", "name"]);
const CUSTOMER_LIST_KEYS = Object.freeze([
  "window", "items", "total", "limit", "offset", "request_id", "trace_id",
]);
const CUSTOMER_ENVELOPE_KEYS = Object.freeze(["customer", "request_id", "trace_id"]);
const WINDOW_KEYS = Object.freeze(["start_date", "end_date", "timezone"]);
const CUSTOMER_KEYS = Object.freeze([
  "partner_customer_id", "project_id", "customer_code", "display_name", "status",
  "commission_rate_bps", "revision", "linked_from_ledger_sequence", "commission_cutoff_sequence",
  "created_at", "updated_at", "disabled_at", "usage", "commission_cursor",
]);
const USAGE_KEYS = Object.freeze([
  "usage_scope", "start_date", "end_date", "request_count", "success_count", "failed_count",
  "input_tokens", "output_tokens", "total_tokens", "cache_tokens", "cost_cny_micros",
  "cost_usd_micros", "estimated_records",
]);
const CUSTOMER_CURSOR_KEYS = Object.freeze([
  "status", "last_scanned_ledger_sequence", "source_high_water_sequence", "freshness_status",
]);
const COMMISSION_KEYS = Object.freeze([
  "window", "items", "totals", "cursor", "total", "limit", "offset", "request_id", "trace_id",
]);
const COMMISSION_LINE_KEYS = Object.freeze([
  "commission_record_id", "partner_customer_id", "customer_code", "customer_name", "currency",
  "amount_micros", "commission_rate_bps", "commission_amount_micros", "input_tokens", "output_tokens",
  "total_tokens", "cache_tokens", "estimated", "status", "settlement_status", "source_request_id",
  "source_trace_id", "source_created_at",
]);
const COMMISSION_TOTAL_KEYS = Object.freeze([
  "currency", "request_count", "total_tokens", "amount_micros", "commission_amount_micros",
  "estimated_records",
]);
const COMMISSION_CURSOR_KEYS = Object.freeze([
  "materialized_through_sequence", "source_high_water_sequence", "freshness_status",
]);

const PARTNER_ID = /^par_[a-f0-9]{32}$/;
const CUSTOMER_ID = /^pcu_[a-f0-9]{32}$/;
const PROJECT_ID = /^prj_[a-f0-9]{32}$/;
const RECORD_ID = /^[A-Za-z][A-Za-z0-9_-]{2,127}$/;
const SAFE_ID = /^[A-Za-z][A-Za-z0-9_.:-]{1,159}$/;
const BUSINESS_CODE = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const COLOR = /^#[0-9A-F]{6}$/;
const UTC_SECOND = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const CURRENCIES = new Set(["CNY", "USD"]);
const MAX_CSV_BYTES = 8 * 1024 * 1024;
const MAX_DELIVERY_BYTES = 1024 * 1024;
const FIXED_ERRORS = Object.freeze({
  "partner.request_invalid": "伙伴门户请求不合法",
  "partner.not_configured": "当前租户尚未配置伙伴门户",
  "partner.already_configured": "当前租户已经配置伙伴门户",
  "partner.revision_conflict": "数据已被更新，请刷新后重试",
  "partner.project_not_found": "可映射项目不存在",
  "partner.customer_not_found": "伙伴客户不存在",
  "partner.customer_conflict": "客户代码或项目已经被占用",
  "partner.commission_unavailable": "分润报告暂不可用",
  "partner.export_too_large": "导出范围过大，请缩小日期范围",
  "partner.export_unavailable": "安全附件暂不可用",
  "partner.event_unavailable": "客户事件暂不可用",
  "partner.unavailable": "伙伴门户暂不可用",
  "partner.response_invalid": "伙伴门户响应不符合安全合同",
  "partner.attachment_invalid": "附件响应不符合安全合同",
});

/** 配置当前租户伙伴门户；边界：后端负责权限、幂等和敏感字段过滤。 */
export async function configurePartner({ controlApiUrl, token, input, idempotencyKey, signal }) {
  const body = normalizePartnerCreateInput(input);
  return profileRequest(`${controlApiUrl}/partners`, {
    method: "POST",
    headers: mutationHeaders(token, { idempotencyKey }),
    body: JSON.stringify(body),
    signal,
  });
}

/** 读取当前租户伙伴资料，并校验统一响应信封。 */
export async function getPartnerProfile({ controlApiUrl, token, signal }) {
  return profileRequest(`${controlApiUrl}/partners/profile`, {
    headers: authorizationHeaders(token), signal,
  });
}

/** 以 revision CAS 更新白标资料，拒绝客户端绕过后端授权。 */
export async function updatePartnerBranding({ controlApiUrl, token, input, revision, signal }) {
  const body = normalizeBrandingInput(input);
  const expectedRevision = requirePositiveInteger(revision, "revision");
  return profileRequest(`${controlApiUrl}/partners/branding`, {
    method: "PATCH",
    headers: mutationHeaders(token, { revision: expectedRevision }),
    body: JSON.stringify(body),
    signal,
  });
}

/** 读取当前租户项目映射列表，并执行精确键校验。 */
export async function listPartnerProjects({ controlApiUrl, token, signal }) {
  const payload = await jsonRequest(`${controlApiUrl}/partners/projects`, {
    headers: authorizationHeaders(token), signal,
  });
  const normalized = normalizePartnerProjectsResponse(payload);
  if (!normalized) throw clientError("partner.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** 读取当前租户客户分页，拒绝越界查询参数。 */
export async function listPartnerCustomers({ controlApiUrl, token, filters = {}, signal }) {
  const query = customerQuery(filters);
  const payload = await jsonRequest(`${controlApiUrl}/partners/customers${query}`, {
    headers: authorizationHeaders(token), signal,
  });
  const normalized = normalizePartnerCustomersResponse(payload);
  if (!normalized) throw clientError("partner.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** 创建客户映射；请求使用后端校验的幂等键。 */
export async function createPartnerCustomer({ controlApiUrl, token, input, idempotencyKey, signal }) {
  const body = normalizeCustomerCreateInput(input);
  const payload = await jsonRequest(`${controlApiUrl}/partners/customers`, {
    method: "POST",
    headers: mutationHeaders(token, { idempotencyKey }),
    body: JSON.stringify(body),
    signal,
  });
  return customerEnvelope(payload);
}

/** 禁用客户映射并接受后端返回的分润 cutoff。 */
export async function updatePartnerCustomer({
  controlApiUrl, token, partnerCustomerId, input, revision, signal,
}) {
  const customerId = requirePattern(partnerCustomerId, CUSTOMER_ID, "partner_customer_id");
  const body = normalizeCustomerUpdateInput(input);
  const expectedRevision = requirePositiveInteger(revision, "revision");
  const payload = await jsonRequest(
    `${controlApiUrl}/partners/customers/${encodeURIComponent(customerId)}`,
    {
      method: "PATCH",
      headers: mutationHeaders(token, { revision: expectedRevision }),
      body: JSON.stringify(body),
      signal,
    },
  );
  return customerEnvelope(payload);
}

/** 读取租户内分润报表，保留新鲜度和成本追踪字段。 */
export async function getPartnerCommission({ controlApiUrl, token, filters = {}, signal }) {
  const query = commissionQuery(filters, false);
  const payload = await jsonRequest(`${controlApiUrl}/partners/commission${query}`, {
    headers: authorizationHeaders(token), signal,
  });
  const normalized = normalizePartnerCommissionResponse(payload);
  if (!normalized) throw clientError("partner.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** 下载受大小限制的分润 CSV，正文不写入日志。 */
export async function downloadPartnerCommissionCsv({ controlApiUrl, token, partnerCode, filters = {}, signal }) {
  const code = requirePattern(partnerCode, BUSINESS_CODE, "partner_code");
  const query = commissionQuery(filters, true);
  return attachmentRequest(`${controlApiUrl}/partners/commission${query}`, {
    token,
    signal,
    kind: "commission",
    expectedContentType: "text/csv; charset=utf-8",
    filenamePattern: new RegExp(`^partner-commission-${escapeRegex(code)}-\\d{4}-\\d{2}-\\d{2}\\.csv$`),
    maxBytes: MAX_CSV_BYTES,
    requireBom: true,
  });
}

/** 下载白标交付包，客户端只接受固定媒体类型和文件名。 */
export async function downloadPartnerDeliveryPackage({ controlApiUrl, token, partnerCode, signal }) {
  const code = requirePattern(partnerCode, BUSINESS_CODE, "partner_code");
  return attachmentRequest(`${controlApiUrl}/partners/delivery-package`, {
    token,
    signal,
    kind: "delivery",
    expectedContentType: "application/json; charset=utf-8",
    filenamePattern: new RegExp(`^metahub-partner-delivery-${escapeRegex(code)}-\\d{4}-\\d{2}-\\d{2}\\.json$`),
    maxBytes: MAX_DELIVERY_BYTES,
    requireBom: false,
  });
}

/** 将已验证附件交给浏览器下载，并在完成后撤销对象 URL。 */
export function triggerPartnerAttachmentDownload(result, environment = globalThis) {
  const urlApi = environment?.URL;
  const documentApi = environment?.document;
  if (
    !result || !["commission", "delivery"].includes(result.kind)
    || !(result.blob instanceof Blob) || result.blob.size < 1
    || !/^[A-Za-z0-9._-]+-\d{4}-\d{2}-\d{2}\.(?:csv|json)$/.test(result.filename)
    || typeof urlApi?.createObjectURL !== "function"
    || typeof urlApi?.revokeObjectURL !== "function"
    || typeof documentApi?.createElement !== "function" || !documentApi.body
  ) throw clientError("partner.attachment_invalid", 502);
  const link = documentApi.createElement("a");
  const objectUrl = urlApi.createObjectURL(result.blob);
  try {
    link.href = objectUrl;
    link.download = result.filename;
    link.rel = "noopener";
    link.hidden = true;
    documentApi.body.append(link);
    link.click();
  } finally {
    link.remove?.();
    urlApi.revokeObjectURL(objectUrl);
  }
}

/** 校验伙伴资料响应的精确键、租户信封和只读字段。 */
export function normalizePartnerProfileResponse(payload) {
  if (!hasExactKeys(payload, PROFILE_KEYS) || !validEnvelope(payload)) return null;
  const partner = normalizePartner(payload.partner);
  return partner ? freezeEnvelope({ partner }, payload) : null;
}

/** 校验项目列表响应并冻结客户端快照。 */
export function normalizePartnerProjectsResponse(payload) {
  if (!hasExactKeys(payload, PROJECT_LIST_KEYS) || !validEnvelope(payload)) return null;
  if (!Array.isArray(payload.items) || payload.items.length > 100 || payload.total !== payload.items.length) return null;
  const items = payload.items.map(normalizeProject);
  if (items.some((item) => !item) || !unique(items, "project_id")) return null;
  return freezeEnvelope({ items: Object.freeze(items), total: payload.total }, payload);
}

/** 校验客户列表响应并消除重复标识。 */
export function normalizePartnerCustomersResponse(payload) {
  if (!hasExactKeys(payload, CUSTOMER_LIST_KEYS) || !validEnvelope(payload)) return null;
  const window = normalizeWindow(payload.window);
  if (!window || !validPage(payload, 100) || !Array.isArray(payload.items)) return null;
  const items = payload.items.map((item) => normalizeCustomer(item, window));
  if (items.some((item) => !item) || !unique(items, "partner_customer_id")) return null;
  return freezeEnvelope({
    window, items: Object.freeze(items), total: payload.total, limit: payload.limit, offset: payload.offset,
  }, payload);
}

/** 校验单个客户响应信封。 */
export function normalizePartnerCustomerResponse(payload) {
  if (!hasExactKeys(payload, CUSTOMER_ENVELOPE_KEYS) || !validEnvelope(payload)) return null;
  const customer = normalizeCustomer(payload.customer);
  return customer ? freezeEnvelope({ customer }, payload) : null;
}

/** 校验分润报表响应、币种总计和游标新鲜度。 */
export function normalizePartnerCommissionResponse(payload) {
  if (!hasExactKeys(payload, COMMISSION_KEYS) || !validEnvelope(payload)) return null;
  const window = normalizeWindow(payload.window);
  const cursor = normalizeCommissionCursor(payload.cursor);
  if (!window || !cursor || !validPage(payload, 100) || !Array.isArray(payload.items) || !Array.isArray(payload.totals)) return null;
  const items = payload.items.map(normalizeCommissionLine);
  const totals = payload.totals.map(normalizeCommissionTotal);
  if (
    items.some((item) => !item) || totals.some((item) => !item)
    || !unique(items, "commission_record_id") || !unique(totals, "currency")
  ) return null;
  return freezeEnvelope({
    window, items: Object.freeze(items), totals: Object.freeze(totals), cursor,
    total: payload.total, limit: payload.limit, offset: payload.offset,
  }, payload);
}

function normalizePartner(value) {
  if (
    !hasExactKeys(value, PARTNER_KEYS) || !PARTNER_ID.test(String(value.partner_id ?? ""))
    || !BUSINESS_CODE.test(String(value.partner_code ?? ""))
    || !validText(value.display_name, 1, 120) || !validText(value.portal_name, 1, 80)
    || !COLOR.test(String(value.brand_primary_color ?? ""))
    || !(value.support_text === null || validText(value.support_text, 0, 160))
    || value.status !== "active" || value.default_commission_rate_bps !== 1000
    || !positiveInteger(value.revision) || !UTC_SECOND.test(String(value.created_at ?? ""))
    || !UTC_SECOND.test(String(value.updated_at ?? ""))
  ) return null;
  return Object.freeze({ ...value });
}

function normalizeProject(value) {
  if (
    !hasExactKeys(value, PROJECT_KEYS) || !PROJECT_ID.test(String(value.project_id ?? ""))
    || !BUSINESS_CODE.test(String(value.project_code ?? "")) || !validText(value.name, 1, 120)
  ) return null;
  return Object.freeze({ ...value });
}

function normalizeCustomer(value, expectedWindow = null) {
  if (
    !hasExactKeys(value, CUSTOMER_KEYS) || !CUSTOMER_ID.test(String(value.partner_customer_id ?? ""))
    || !PROJECT_ID.test(String(value.project_id ?? "")) || !BUSINESS_CODE.test(String(value.customer_code ?? ""))
    || !validText(value.display_name, 1, 120) || !["active", "disabled"].includes(value.status)
    || value.commission_rate_bps !== 1000 || !positiveInteger(value.revision)
    || !nonNegativeInteger(value.linked_from_ledger_sequence)
    || !(value.commission_cutoff_sequence === null || nonNegativeInteger(value.commission_cutoff_sequence))
    || !UTC_SECOND.test(String(value.created_at ?? "")) || !UTC_SECOND.test(String(value.updated_at ?? ""))
    || !(value.disabled_at === null || UTC_SECOND.test(String(value.disabled_at ?? "")))
    || (value.status === "active" && (value.commission_cutoff_sequence !== null || value.disabled_at !== null))
    || (value.status === "disabled" && (value.commission_cutoff_sequence === null || value.disabled_at === null))
  ) return null;
  const usage = normalizeUsage(value.usage, expectedWindow);
  const commissionCursor = normalizeCustomerCursor(value.commission_cursor);
  if (!usage || !commissionCursor) return null;
  if (
    commissionCursor.status === "complete"
    && (
      value.status !== "disabled"
      || value.commission_cutoff_sequence === null
      || commissionCursor.last_scanned_ledger_sequence !== value.commission_cutoff_sequence
      || commissionCursor.source_high_water_sequence !== value.commission_cutoff_sequence
      || commissionCursor.freshness_status !== "current"
    )
  ) return null;
  return Object.freeze({ ...value, usage, commission_cursor: commissionCursor });
}

function normalizeUsage(value, expectedWindow) {
  if (!hasExactKeys(value, USAGE_KEYS) || value.usage_scope !== "project_history") return null;
  if (
    !DATE.test(String(value.start_date ?? "")) || !DATE.test(String(value.end_date ?? ""))
    || (expectedWindow && (value.start_date !== expectedWindow.start_date || value.end_date !== expectedWindow.end_date))
  ) return null;
  const fields = USAGE_KEYS.slice(3);
  if (fields.some((field) => !nonNegativeInteger(value[field]))) return null;
  if (value.success_count + value.failed_count !== value.request_count) return null;
  if (value.input_tokens + value.output_tokens !== value.total_tokens) return null;
  return Object.freeze({ ...value });
}

function normalizeCustomerCursor(value) {
  if (
    !hasExactKeys(value, CUSTOMER_CURSOR_KEYS) || !["pending", "leased", "complete"].includes(value.status)
    || !nonNegativeInteger(value.last_scanned_ledger_sequence)
    || !nonNegativeInteger(value.source_high_water_sequence)
    || !["current", "lagging"].includes(value.freshness_status)
    || value.last_scanned_ledger_sequence > value.source_high_water_sequence
  ) return null;
  return Object.freeze({ ...value });
}

function normalizeCommissionLine(value) {
  if (
    !hasExactKeys(value, COMMISSION_LINE_KEYS)
    || !RECORD_ID.test(String(value.commission_record_id ?? ""))
    || !CUSTOMER_ID.test(String(value.partner_customer_id ?? ""))
    || !BUSINESS_CODE.test(String(value.customer_code ?? "")) || !validText(value.customer_name, 1, 120)
    || !CURRENCIES.has(value.currency) || !nonNegativeInteger(value.amount_micros)
    || value.commission_rate_bps !== 1000 || !nonNegativeInteger(value.commission_amount_micros)
    || !nonNegativeInteger(value.input_tokens) || !nonNegativeInteger(value.output_tokens)
    || value.input_tokens + value.output_tokens !== value.total_tokens
    || !nonNegativeInteger(value.cache_tokens) || typeof value.estimated !== "boolean"
    || value.status !== "accrued" || value.settlement_status !== "not_applicable"
    || !SAFE_ID.test(String(value.source_request_id ?? "")) || !SAFE_ID.test(String(value.source_trace_id ?? ""))
    || !UTC_SECOND.test(String(value.source_created_at ?? ""))
  ) return null;
  return Object.freeze({ ...value });
}

function normalizeCommissionTotal(value) {
  if (!hasExactKeys(value, COMMISSION_TOTAL_KEYS) || !CURRENCIES.has(value.currency)) return null;
  if (COMMISSION_TOTAL_KEYS.slice(1).some((field) => !nonNegativeInteger(value[field]))) return null;
  return Object.freeze({ ...value });
}

function normalizeCommissionCursor(value) {
  if (
    !hasExactKeys(value, COMMISSION_CURSOR_KEYS)
    || !nonNegativeInteger(value.materialized_through_sequence)
    || !nonNegativeInteger(value.source_high_water_sequence)
    || !["current", "lagging"].includes(value.freshness_status)
    || value.materialized_through_sequence > value.source_high_water_sequence
  ) return null;
  return Object.freeze({ ...value });
}

function normalizeWindow(value) {
  if (
    !hasExactKeys(value, WINDOW_KEYS) || !DATE.test(String(value.start_date ?? ""))
    || !DATE.test(String(value.end_date ?? "")) || value.timezone !== "UTC"
    || !validDateWindow(value.start_date, value.end_date)
  ) return null;
  return Object.freeze({ ...value });
}

async function profileRequest(url, options) {
  const payload = await jsonRequest(url, options);
  const normalized = normalizePartnerProfileResponse(payload);
  if (!normalized) throw clientError("partner.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

function customerEnvelope(payload) {
  const normalized = normalizePartnerCustomerResponse(payload);
  if (!normalized) throw clientError("partner.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

async function jsonRequest(url, options) {
  let response;
  try {
    response = await fetch(url, {
      ...options, cache: "no-store", credentials: "omit", redirect: "error",
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError("partner.unavailable", 503);
  }
  const payload = await readJson(response);
  if (!response.ok) throw responseError(response, payload);
  const contentType = String(response.headers.get("Content-Type") ?? "").toLowerCase();
  const cacheControl = String(response.headers.get("Cache-Control") ?? "").toLowerCase();
  if (!contentType.startsWith("application/json") || !privateNoStore(cacheControl)) {
    throw clientError("partner.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return payload;
}

async function attachmentRequest(url, options) {
  let response;
  try {
    response = await fetch(url, {
      headers: { Authorization: `Bearer ${requireToken(options.token)}`, Accept: options.expectedContentType.split(";")[0] },
      signal: options.signal, cache: "no-store", credentials: "omit", redirect: "error",
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError("partner.export_unavailable", 503);
  }
  if (!response.ok) throw responseError(response, await readJson(response));
  const contentType = String(response.headers.get("Content-Type") ?? "").toLowerCase();
  const disposition = String(response.headers.get("Content-Disposition") ?? "");
  const cacheControl = String(response.headers.get("Cache-Control") ?? "").toLowerCase();
  const requestId = safeChain(response.headers.get("X-Request-ID"));
  const traceId = safeChain(response.headers.get("X-Trace-ID"));
  const nosniff = String(response.headers.get("X-Content-Type-Options") ?? "").toLowerCase();
  const lengthHeader = response.headers.get("Content-Length");
  const filename = disposition.match(/^attachment;\s*filename="([A-Za-z0-9._-]+)"$/i)?.[1] ?? "";
  const declaredLength = lengthHeader === null ? null : Number(lengthHeader);
  if (
    contentType !== options.expectedContentType || !privateNoStore(cacheControl) || nosniff !== "nosniff"
    || !requestId || !traceId || !options.filenamePattern.test(filename)
    || (declaredLength !== null && (!Number.isSafeInteger(declaredLength) || declaredLength < 1 || declaredLength > options.maxBytes))
  ) throw clientError("partner.attachment_invalid", 502, requestId, traceId);
  const blob = await response.blob();
  if (!(blob instanceof Blob) || blob.size < 1 || blob.size > options.maxBytes || (declaredLength !== null && blob.size !== declaredLength)) {
    throw clientError("partner.attachment_invalid", 502, requestId, traceId);
  }
  if (options.requireBom) {
    const prefix = new Uint8Array(await blob.slice(0, 3).arrayBuffer());
    if (prefix.length !== 3 || prefix[0] !== 0xef || prefix[1] !== 0xbb || prefix[2] !== 0xbf) {
      throw clientError("partner.attachment_invalid", 502, requestId, traceId);
    }
  }
  return Object.freeze({ kind: options.kind, blob, filename, requestId, traceId });
}

function normalizePartnerCreateInput(input) {
  if (!hasExactKeys(input, ["partner_code", "display_name", "portal_name", "brand_primary_color", "support_text"])) {
    throw clientError("partner.request_invalid", 422);
  }
  return Object.freeze({
    partner_code: requirePattern(input.partner_code, BUSINESS_CODE, "partner_code"),
    display_name: requireText(input.display_name, 1, 120),
    portal_name: requireText(input.portal_name, 1, 80),
    brand_primary_color: requireColor(input.brand_primary_color),
    support_text: input.support_text ? requireText(input.support_text, 0, 160) : null,
  });
}

function normalizeBrandingInput(input) {
  if (!hasExactKeys(input, ["portal_name", "brand_primary_color", "support_text"])) {
    throw clientError("partner.request_invalid", 422);
  }
  return Object.freeze({
    portal_name: requireText(input.portal_name, 1, 80),
    brand_primary_color: requireColor(input.brand_primary_color),
    support_text: input.support_text ? requireText(input.support_text, 0, 160) : null,
  });
}

function normalizeCustomerCreateInput(input) {
  if (!hasExactKeys(input, ["project_id", "customer_code", "display_name"])) throw clientError("partner.request_invalid", 422);
  return Object.freeze({
    project_id: requirePattern(input.project_id, PROJECT_ID, "project_id"),
    customer_code: requirePattern(input.customer_code, BUSINESS_CODE, "customer_code"),
    display_name: requireText(input.display_name, 1, 120),
  });
}

function normalizeCustomerUpdateInput(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw clientError("partner.request_invalid", 422);
  const keys = Object.keys(input).sort();
  if (!keys.length || keys.some((key) => !["display_name", "status"].includes(key))) throw clientError("partner.request_invalid", 422);
  const output = {};
  if (Object.hasOwn(input, "display_name")) output.display_name = requireText(input.display_name, 1, 120);
  if (Object.hasOwn(input, "status")) {
    if (input.status !== "disabled") throw clientError("partner.request_invalid", 422);
    output.status = "disabled";
  }
  return Object.freeze(output);
}

function customerQuery(filters) {
  validateDateWindow(filters);
  return buildQuery(filters, ["start_date", "end_date", "status", "q", "limit", "offset"], {
    start_date: dateValue, end_date: dateValue,
    status: (value) => ["active", "disabled"].includes(value) ? value : invalid(),
    q: (value) => requireText(value, 1, 120),
    limit: (value) => requireRangeInteger(value, 1, 100),
    offset: (value) => requireRangeInteger(value, 0, Number.MAX_SAFE_INTEGER),
  });
}

function commissionQuery(filters, csv) {
  validateDateWindow(filters);
  const source = { ...filters, ...(csv ? { format: "csv" } : {}) };
  return buildQuery(source, ["start_date", "end_date", "customer_id", "format", "limit", "offset"], {
    start_date: dateValue, end_date: dateValue,
    customer_id: (value) => requirePattern(value, CUSTOMER_ID, "customer_id"),
    format: (value) => value === "csv" ? value : invalid(),
    limit: (value) => csv ? invalid() : requireRangeInteger(value, 1, 100),
    offset: (value) => csv ? invalid() : requireRangeInteger(value, 0, Number.MAX_SAFE_INTEGER),
  });
}

function buildQuery(values, allowed, validators) {
  if (!values || typeof values !== "object" || Array.isArray(values)) throw clientError("partner.request_invalid", 422);
  if (Object.keys(values).some((key) => !allowed.includes(key))) throw clientError("partner.request_invalid", 422);
  const query = new URLSearchParams();
  for (const key of allowed) {
    const value = values[key];
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(validators[key](value)));
  }
  const text = query.toString();
  return text ? `?${text}` : "";
}

function validateDateWindow(values) {
  if (!values || typeof values !== "object" || Array.isArray(values)) return;
  const start = values.start_date ?? "";
  const end = values.end_date ?? "";
  if (!start && !end) return;
  if (!DATE.test(String(start)) || !DATE.test(String(end))) return invalid();
  if (!validDateWindow(String(start), String(end))) return invalid();
}

function validDateWindow(start, end) {
  const startMs = Date.parse(`${start}T00:00:00Z`);
  const endMs = Date.parse(`${end}T00:00:00Z`);
  return Number.isFinite(startMs) && Number.isFinite(endMs)
    && new Date(startMs).toISOString().slice(0, 10) === start
    && new Date(endMs).toISOString().slice(0, 10) === end
    && endMs >= startMs && endMs - startMs <= 365 * 86400000;
}

function authorizationHeaders(token) {
  return { Authorization: `Bearer ${requireToken(token)}`, Accept: "application/json" };
}

function mutationHeaders(token, { idempotencyKey, revision } = {}) {
  const headers = { ...authorizationHeaders(token), "Content-Type": "application/json" };
  if (idempotencyKey !== undefined) headers["Idempotency-Key"] = requireIdempotencyKey(idempotencyKey);
  if (revision !== undefined) headers["If-Match"] = String(revision);
  return headers;
}

function responseError(response, payload) {
  const code = typeof payload?.code === "string" && payload.code in FIXED_ERRORS ? payload.code : "partner.unavailable";
  return clientError(code, response.status, payload?.request_id, payload?.trace_id);
}

function clientError(code, status, requestId = "-", traceId = "-") {
  return new AuthApiError({
    status, code, message: FIXED_ERRORS[code] ?? FIXED_ERRORS["partner.unavailable"],
    requestId: safeChain(requestId) || "-", traceId: safeChain(traceId) || "-",
  });
}

async function readJson(response) {
  try { return await response.json(); } catch { return {}; }
}

function validEnvelope(payload) {
  return Boolean(safeChain(payload?.request_id) && safeChain(payload?.trace_id));
}

function freezeEnvelope(values, payload) {
  return Object.freeze({ ...values, request_id: payload.request_id, trace_id: payload.trace_id });
}

function validPage(payload, maxItems) {
  return nonNegativeInteger(payload.total) && requireBooleanInteger(payload.limit, 1, maxItems)
    && requireBooleanInteger(payload.offset, 0, Number.MAX_SAFE_INTEGER) && payload.items.length <= payload.limit;
}

function unique(values, field) { return new Set(values.map((value) => value[field])).size === values.length; }
function hasExactKeys(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("\0") === [...keys].sort().join("\0"));
}
function nonNegativeInteger(value) { return Number.isSafeInteger(value) && value >= 0; }
function positiveInteger(value) { return Number.isSafeInteger(value) && value >= 1; }
function requireBooleanInteger(value, min, max) { return Number.isSafeInteger(value) && value >= min && value <= max; }
function validText(value, minBytes, maxBytes) {
  return typeof value === "string" && !/[\u0000-\u001f\u007f]/.test(value)
    && new TextEncoder().encode(value).byteLength >= minBytes
    && new TextEncoder().encode(value).byteLength <= maxBytes;
}
function requireText(value, min, max) {
  const text = String(value ?? "").trim();
  if (!validText(text, min, max)) throw clientError("partner.request_invalid", 422);
  return text;
}
function requirePattern(value, pattern, _field) {
  const text = String(value ?? "");
  if (!pattern.test(text)) throw clientError("partner.request_invalid", 422);
  return text;
}
function requireColor(value) {
  const color = String(value ?? "").toUpperCase();
  if (!COLOR.test(color)) throw clientError("partner.request_invalid", 422);
  return color;
}
function requirePositiveInteger(value) {
  if (!positiveInteger(Number(value))) throw clientError("partner.request_invalid", 422);
  return Number(value);
}
function requireRangeInteger(value, min, max) {
  const number = Number(value);
  if (!requireBooleanInteger(number, min, max)) throw clientError("partner.request_invalid", 422);
  return number;
}
function requireToken(token) {
  if (typeof token !== "string" || !token) throw clientError("partner.request_invalid", 422);
  return token;
}
function requireIdempotencyKey(value) {
  const text = String(value ?? "");
  if (!/^[A-Za-z0-9._:-]{16,160}$/.test(text)) throw clientError("partner.request_invalid", 422);
  return text;
}
function dateValue(value) {
  const text = String(value ?? "");
  if (!DATE.test(text)) return invalid();
  return text;
}
function invalid() { throw clientError("partner.request_invalid", 422); }
function safeChain(value) { return SAFE_ID.test(String(value ?? "")) ? String(value) : ""; }
function privateNoStore(value) { return value.split(",").map((item) => item.trim()).includes("no-store") && value.includes("private"); }
function escapeRegex(value) { return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

/** 附件大小上限与后端合同保持一致，避免前端绕过安全边界。 */
export const PARTNER_ATTACHMENT_LIMITS = Object.freeze({ csv: MAX_CSV_BYTES, delivery: MAX_DELIVERY_BYTES });
