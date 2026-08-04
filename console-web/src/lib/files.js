/**
 * 模块职责：封装 PAGE-101/PAGE-102 文件上传、列表与安全 DTO 投影。
 * 安全边界：浏览器只提交一个原生 File；tenant、actor、对象键、DLP 规则和抽取正文均不得
 * 进入 URL、表单附加字段、错误对象或页面状态，真实 RBAC 与租户隔离由 Control API 强制执行。
 */

import { AuthApiError } from "./auth.js";

// 当前 PAGE-101 冻结默认 20 MiB 体验上限；后端仍会按部署配置再次执行权威字节限制。
export const FILE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024;

// 文件 owner 只允许四个持久状态，未知值不得进入页面排序或颜色语义。
const FILE_STATUSES = new Set(["processing", "ready", "blocked", "failed"]);
// DLP 与文件状态是独立维度，pending/unavailable 不能被浏览器误报为 clean。
const DLP_STATUSES = new Set(["pending", "clean", "matched", "blocked", "unavailable"]);
// 公开响应只允许有效规则决策，不接受任意规则名称或自由文本。
const DLP_ACTIONS = new Set(["allow", "log_only", "block"]);
// 抽取记录没有 processing 状态；它只在文件形成终态后出现。
const EXTRACT_STATUSES = new Set(["ready", "blocked", "failed"]);
// 前端选择器与后端白名单保持一致，但服务端仍负责 MIME/魔数权威校验。
const EXTENSIONS = new Set(["txt", "docx", "pdf"]);
// 扩展名与 MIME 一一对应，禁止以 octet-stream 绕过浏览器合同。
const MEDIA_TYPES = Object.freeze({
  txt: "text/plain",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  pdf: "application/pdf",
});
// 解析器版本必须与格式一致，防止响应把其他解析链路结果冒充当前文件。
const EXTRACTOR_VERSIONS = Object.freeze({
  txt: "txt-v1",
  docx: "docx-v1",
  pdf: "pdf-v1",
});
// 危险早期后缀会造成双扩展显示欺骗，选择器和响应投影都应失败关闭。
const DANGEROUS_EARLIER_SUFFIXES = new Set([
  "bat", "cmd", "com", "exe", "html", "hta", "jar", "js", "lnk", "msi", "ps1", "scr", "sh", "svg", "vbs",
]);

// exact-key 合同确保未来后端字段不会把正文、规则 ID、hash 或对象存储路径静默带入浏览器状态。
const LIST_KEYS = Object.freeze(["files", "next_cursor", "request_id", "trace_id"]);
// 单文件信封只允许安全 FileInfo 与链路标识。
const RESPONSE_KEYS = Object.freeze(["file", "request_id", "trace_id"]);
// FileInfo 白名单刻意排除正文、规则身份、fingerprint 和存储定位字段。
const FILE_KEYS = Object.freeze([
  "file_id",
  "original_name",
  "file_extension",
  "media_type",
  "size_bytes",
  "status",
  "virus_scan_status",
  "dlp_scan_status",
  "dlp_action",
  "match_count",
  "failure_error_code",
  "upload_request_id",
  "extract",
  "created_at",
  "updated_at",
  "completed_at",
]);
// FileExtract 只公开计数、版本和固定失败码，正文与对象键没有兼容入口。
const EXTRACT_KEYS = Object.freeze([
  "file_extract_id",
  "status",
  "extractor_version",
  "text_size_bytes",
  "text_code_points",
  "estimated_tokens",
  "estimator_version",
  "failure_error_code",
]);

// 页面错误文案只由固定 code 决定，服务端自由 message 可能含文件名或底层解析信息，禁止直接展示。
const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有文件操作权限",
  "infra.validation_failed": "文件筛选或分页游标无效",
  "file.multipart_invalid": "文件上传请求格式无效",
  "file.name_invalid": "文件名不符合安全要求",
  "file.unsupported_format": "仅支持 PDF、DOCX 和 UTF-8 TXT",
  "file.too_large": "文件超过 20 MiB 上限",
  "file.processing_busy": "文件解析任务繁忙，请稍后重试",
  "file.structure_invalid": "文件结构无效或包含不受支持的内容",
  "file.no_extractable_text": "文件中没有可提取文本",
  "file.extract_limit_exceeded": "文件页数或抽取文本超过安全上限",
  "file.parse_failed": "文件文本解析失败",
  "file.dlp_blocked": "文件命中当前租户 DLP 阻断规则",
  "file.dlp_unavailable": "DLP 预扫描暂不可用，文件已失败关闭",
  "file.storage_unavailable": "文件存储暂不可用",
  "file.integrity_failed": "文件处理状态完整性校验失败",
  "file.not_found": "当前租户中找不到该文件",
  "file.response_invalid": "文件响应合同不完整",
});

/**
 * 读取当前 tenant 的文件处理记录。
 *
 * @param {object} options Control 地址、Session Token、固定筛选、私有 cursor 与 AbortSignal。
 * @returns {Promise<object>} exact-key 校验并深度冻结的文件列表。
 *
 * tenant_id、user_id、排序和对象存储字段即使由调用方附加，也不会进入查询字符串。
 */
export async function listFiles({
  controlApiUrl,
  token,
  status = "",
  limit = 25,
  cursor = null,
  signal,
}) {
  const url = new URL(`${controlApiUrl}/files`);
  const normalizedStatus = String(status ?? "").trim();
  if (normalizedStatus) {
    // 未知状态不能进入 URL，否则浏览器可能与后端分页签名采用不同筛选语义。
    if (!FILE_STATUSES.has(normalizedStatus)) throw fileClientError("file.multipart_invalid", 422);
    url.searchParams.set("status", normalizedStatus);
  }
  // 分页数量必须在服务端合同范围内，禁止浏览器用大值扩大响应和文件名内存占用。
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
    throw fileClientError("file.multipart_invalid", 422);
  }
  url.searchParams.set("limit", String(limit));
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    const normalizedCursor = String(cursor);
    // cursor 虽不进入 DOM，仍须在发请求前限制长度和控制字符，防止 URL 注入。
    if (!safeCursor(normalizedCursor)) throw fileClientError("file.multipart_invalid", 422);
    url.searchParams.set("cursor", normalizedCursor);
  }

  const payload = await fileRequest(url.toString(), {
    headers: authorizationHeaders(token),
    signal,
  });
  const normalized = normalizeFileListResponse(payload);
  // 成功 HTTP 但合同漂移时拒绝整页，不能把部分可信字段交给 renderer。
  if (!normalized) throw fileClientError("file.response_invalid", 502, payload?.request_id);
  return normalized;
}

/**
 * 读取当前 tenant 的单个文件安全摘要。
 *
 * @param {object} options Control 地址、Session Token、规范 file ID 与 AbortSignal。
 * @returns {Promise<object>} 与 POST 相同的 FileResponse 安全投影。
 *
 * file ID 只作为受限路径段；tenant 仍只能由后端 Session 恢复，跨租户与不存在保持同形 404。
 */
export async function getFile({ controlApiUrl, token, fileId, signal }) {
  const normalizedFileId = String(fileId ?? "").trim();
  // 非规范路径 ID 在出网前同形拒绝，避免斜杠或编码变体探测其他资源。
  if (!/^file_[0-9a-f]{32}$/.test(normalizedFileId)) {
    throw fileClientError("file.not_found", 404);
  }
  const payload = await fileRequest(
    `${controlApiUrl}/files/${encodeURIComponent(normalizedFileId)}`,
    { headers: authorizationHeaders(token), signal },
  );
  const normalized = normalizeFileResponse(payload);
  // 详情成功响应同样执行 exact-key 校验，不能比列表放宽隐私字段。
  if (!normalized) throw fileClientError("file.response_invalid", 502, payload?.request_id);
  return normalized;
}

/**
 * 上传单个文件并同步等待文本解析与安全预扫描终态。
 *
 * @param {object} options Control 地址、Session Token、原生 File 和 AbortSignal。
 * @returns {Promise<object>} 只含 FileInfo 安全投影的成功响应。
 *
 * FormData 恰好包含一个 ``file`` part；代码不得手工设置 Content-Type/boundary，也不得自动重试。
 */
export async function uploadFile({ controlApiUrl, token, file, signal }) {
  validateSelectedFile(file);
  const body = new FormData();
  // 文件名只能作为 multipart 标准 filename 交给后端；不得复制到 query、自定义 Header 或错误消息。
  body.append("file", file, file.name);
  const payload = await fileRequest(`${controlApiUrl}/files`, {
    method: "POST",
    headers: authorizationHeaders(token),
    body,
    signal,
  });
  const normalized = normalizeFileResponse(payload);
  // 上传成功不代表任意响应都可信，公开 DTO 不完整时仍失败关闭。
  if (!normalized) throw fileClientError("file.response_invalid", 502, payload?.request_id);
  return normalized;
}

/** 按根 exact-key 合同投影列表；cursor 保留给页面私有闭包，不得交给 renderer。 */
export function normalizeFileListResponse(payload) {
  if (
    !hasExactKeys(payload, LIST_KEYS)
    || !Array.isArray(payload.files)
    || payload.files.length > 100
    || !safeNullableCursor(payload.next_cursor)
    || !safeId(payload.request_id)
    || !safeId(payload.trace_id)
  ) return null;
  const files = payload.files.map(normalizeFileInfo);
  if (files.some((item) => !item)) return null;
  const ids = files.map((item) => item.file_id);
  if (new Set(ids).size !== ids.length) return null;
  return Object.freeze({
    state: "ok",
    files: Object.freeze(files),
    next_cursor: payload.next_cursor,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 按 POST/详情根合同投影单个文件响应。 */
export function normalizeFileResponse(payload) {
  if (
    !hasExactKeys(payload, RESPONSE_KEYS)
    || !safeId(payload.request_id)
    || !safeId(payload.trace_id)
  ) return null;
  const file = normalizeFileInfo(payload.file);
  return file
    ? Object.freeze({
        state: "ok",
        file,
        request_id: payload.request_id,
        trace_id: payload.trace_id,
      })
    : null;
}

/** 返回选择器文件名是否具有本页面支持的单一扩展名；最终格式仍由后端 MIME/魔数校验。 */
export function isSupportedUploadName(value) {
  const normalized = String(value ?? "").normalize("NFC");
  if (!safeFilename(normalized)) return false;
  // JavaScript 没有原生 rsplit；用最后一个点明确拒绝无 basename 或无 suffix 的名称。
  const separator = normalized.lastIndexOf(".");
  return Boolean(
    separator > 0
    && separator < normalized.length - 1
    && EXTENSIONS.has(normalized.slice(separator + 1).toLowerCase()),
  );
}

/** 统一发起 no-store 文件请求，并把未知网络/响应错误折叠为不含文件名的固定异常。 */
async function fileRequest(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      cache: "no-store",
      credentials: "omit",
      headers: options.headers ?? {},
    });
  } catch (error) {
    // 主动 Abort 属于 owner 生命周期控制，必须原样交给状态机静默丢弃。
    if (error?.name === "AbortError") throw error;
    // 网络异常对象可能携带代理 URL 或本机细节，只转换为固定存储不可用错误。
    throw fileClientError("file.storage_unavailable", 503);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = safeErrorCode(payload.code) || "file.storage_unavailable";
    // 只保留统一 code 和链路 ID，明确丢弃可能含文件名或解析器详情的服务端 message。
    throw fileClientError(
      code,
      response.status,
      payload.request_id || response.headers.get("X-Request-ID"),
      payload.trace_id || response.headers.get("X-Trace-ID"),
    );
  }
  return payload;
}

/** 校验原生 File 的本地低成本边界；文件 MIME、魔数和深层结构仍完全以后端为准。 */
function validateSelectedFile(file) {
  if (
    !file
    || typeof file.name !== "string"
    || !Number.isSafeInteger(file.size)
    || file.size < 1
    || typeof file.arrayBuffer !== "function"
  ) throw fileClientError("file.multipart_invalid", 400);
  // 扩展名预检只改善体验；失败仍使用固定错误且不回显文件名。
  if (!isSupportedUploadName(file.name)) throw fileClientError("file.unsupported_format", 415);
  // 20 MiB 是本页面冻结上限，后端还会按真实流字节再次校验，Header 不能替代该边界。
  if (file.size > FILE_UPLOAD_MAX_BYTES) throw fileClientError("file.too_large", 413);
}

/** 深度校验 FileInfo，拒绝任何未列入公开合同的字段和不自洽状态。 */
function normalizeFileInfo(value) {
  if (!hasExactKeys(value, FILE_KEYS)) return null;
  const extension = String(value.file_extension ?? "");
  const extract = value.extract === null ? null : normalizeExtract(value.extract, extension);
  if (
    !/^file_[0-9a-f]{32}$/.test(String(value.file_id ?? ""))
    || !safeFilename(value.original_name)
    || !EXTENSIONS.has(extension)
    || value.media_type !== MEDIA_TYPES[extension]
    || !safeInteger(value.size_bytes, 1, 50 * 1024 * 1024)
    || !FILE_STATUSES.has(value.status)
    || value.virus_scan_status !== "not_configured"
    || !DLP_STATUSES.has(value.dlp_scan_status)
    || !(value.dlp_action === null || DLP_ACTIONS.has(value.dlp_action))
    || !safeInteger(value.match_count, 0, 100)
    || !safeNullableErrorCode(value.failure_error_code)
    || !safeId(value.upload_request_id)
    || (value.extract !== null && !extract)
    || !safeIsoDateTime(value.created_at)
    || !safeIsoDateTime(value.updated_at)
    || !safeNullableIsoDateTime(value.completed_at)
  ) return null;
  // processing 是唯一非终态；它不能伪造抽取结果、完成时间或失败原因。
  if (
    value.status === "processing"
    && (
      extract !== null
      || value.completed_at !== null
      || value.failure_error_code !== null
      || value.dlp_scan_status !== "pending"
      || value.dlp_action !== null
      || value.match_count !== 0
    )
  ) return null;
  if (value.status !== "processing" && value.completed_at === null) return null;
  if (value.status === "ready" && (!extract || extract.status !== "ready")) return null;
  if (value.status === "blocked" && (!extract || extract.status !== "blocked")) return null;
  if (value.status === "failed" && extract && extract.status !== "failed") return null;
  if (value.status === "blocked" && value.dlp_action !== "block") return null;
  return Object.freeze({
    file_id: value.file_id,
    original_name: value.original_name,
    file_extension: extension,
    media_type: value.media_type,
    size_bytes: value.size_bytes,
    status: value.status,
    virus_scan_status: "not_configured",
    dlp_scan_status: value.dlp_scan_status,
    dlp_action: value.dlp_action,
    match_count: value.match_count,
    failure_error_code: value.failure_error_code,
    upload_request_id: value.upload_request_id,
    extract,
    created_at: value.created_at,
    updated_at: value.updated_at,
    completed_at: value.completed_at,
  });
}

/** 校验 FileExtract 安全计数；正文、fingerprint 和对象键不在本函数可接纳字段中。 */
function normalizeExtract(value, extension) {
  if (!hasExactKeys(value, EXTRACT_KEYS)) return null;
  const terminalWithCounts = value.status === "ready" || value.status === "blocked";
  if (
    !/^fext_[0-9a-f]{32}$/.test(String(value.file_extract_id ?? ""))
    || !EXTRACT_STATUSES.has(value.status)
    || value.extractor_version !== EXTRACTOR_VERSIONS[extension]
    || !safeNullableInteger(value.text_size_bytes, 0, 8 * 1024 * 1024)
    || !safeNullableInteger(value.text_code_points, 0, 4_000_000)
    || !safeNullableInteger(value.estimated_tokens, 0, Number.MAX_SAFE_INTEGER)
    || !(value.estimator_version === null || value.estimator_version === "file-token-estimator-v1")
    || !safeNullableErrorCode(value.failure_error_code)
  ) return null;
  if (
    terminalWithCounts
    && (
      value.text_size_bytes === null
      || value.text_code_points === null
      || value.estimated_tokens === null
      || value.estimator_version !== "file-token-estimator-v1"
    )
  ) return null;
  // ready 不能携带失败码，否则页面会同时展示成功与失败两种冲突结论。
  if (value.status === "ready" && value.failure_error_code !== null) return null;
  return Object.freeze({
    file_extract_id: value.file_extract_id,
    status: value.status,
    extractor_version: value.extractor_version,
    text_size_bytes: value.text_size_bytes,
    text_code_points: value.text_code_points,
    estimated_tokens: value.estimated_tokens,
    estimator_version: value.estimator_version,
    failure_error_code: value.failure_error_code,
  });
}

/** 认证 Header 只驻留单次请求栈，不复制到响应、页面状态或 DOM。 */
function authorizationHeaders(token) {
  return {
    Accept: "application/json",
    Authorization: `Bearer ${token}`,
  };
}

/** 解析 JSON 失败返回空对象；代理 HTML 或正文不得进入异常 message。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/** 创建只含固定 code、文案和链路 ID 的统一错误。 */
function fileClientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = safeErrorCode(code) || "file.storage_unavailable";
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "文件服务暂不可用",
    requestId: safeId(requestId) || "-",
    traceId: safeId(traceId) || "-",
    suggestion: "",
    diagnosis: null,
  });
}

/** exact-key 比较不依赖属性顺序，并拒绝数组、原型对象和 null。 */
function hasExactKeys(value, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

/** 文件名是唯一允许展示的业务敏感字段，先执行有界、路径和控制字符校验。 */
function safeFilename(value) {
  if (typeof value !== "string" || !value || value !== value.normalize("NFC")) return false;
  if (value === "." || value === ".." || value.includes("/") || value.includes("\\")) return false;
  if (new TextEncoder().encode(value).length > 255) return false;
  if ([...value].some((character) => /[\p{Cc}\p{Cf}]/u.test(character))) return false;
  const suffixes = value.split(".").slice(1, -1).map((item) => item.toLowerCase());
  return !suffixes.some((suffix) => DANGEROUS_EARLIER_SUFFIXES.has(suffix));
}

/** 统一错误码只能使用固定点分命名空间字符。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** request/trace ID 仅允许有界安全 ASCII。 */
function safeId(value) {
  const normalized = String(value ?? "").trim();
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(normalized) ? normalized : "";
}

/** cursor 永不进入 DOM，但仍限制为有界无控制字符文本，避免 URL 注入和内存放大。 */
function safeCursor(value) {
  return typeof value === "string"
    && value.length >= 1
    && value.length <= 2048
    && /^[A-Za-z0-9._~+/=-]+$/.test(value);
}

/** 可空 cursor 仅接受 null 或安全字符串。 */
function safeNullableCursor(value) {
  return value === null || safeCursor(value);
}

/** 可空错误码不允许自由解析器文本进入响应 DTO。 */
function safeNullableErrorCode(value) {
  return value === null || Boolean(safeErrorCode(value));
}

/** 校验闭区间内的安全整数。 */
function safeInteger(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/** 可空整数供抽取前失败使用；零不能自动解释为已成功抽取空文本。 */
function safeNullableInteger(value, minimum, maximum) {
  return value === null || safeInteger(value, minimum, maximum);
}

/** 时间字段必须是可解析的 UTC ISO 文本，避免浏览器按本地时区猜测后端事实。 */
function safeIsoDateTime(value) {
  return typeof value === "string"
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value)
    && !Number.isNaN(Date.parse(value));
}

/** 可空完成时间只允许 null 或规范 UTC ISO。 */
function safeNullableIsoDateTime(value) {
  return value === null || safeIsoDateTime(value);
}
