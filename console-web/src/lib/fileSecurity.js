/**
 * 模块职责：封装 PAGE-103 文件安全报告读取、扫描和 CSV 下载合同。
 * 安全边界：客户端只接收规范 ID、固定枚举、计数和时间；tenant、actor、文件名、正文、OCR
 * 文本、证据指纹、报告 hash、对象键和规则表达式均不允许进入响应投影或异常信息。
 */

import { AuthApiError } from "./auth.js";
import { listFiles } from "./files.js";

// 三个 JSON 入口共享同一 exact-key 信封，新增字段必须显式评审后才能进入浏览器状态。
const RESPONSE_KEYS = Object.freeze(["report", "reused", "request_id", "trace_id"]);
// 报告字段与 Control 的 FileSecurityReport 一一对应，刻意排除 tenant、actor 与所有 evidence。
const REPORT_KEYS = Object.freeze([
  "scan_id",
  "file_id",
  "status",
  "risk_level",
  "risk_score",
  "coverage_status",
  "model_access_status",
  "detector_version",
  "boundary_version",
  "ocr_status",
  "ocr_engine_version",
  "scanned_page_count",
  "scanned_image_count",
  "finding_count",
  "high_finding_count",
  "findings",
  "created_at",
  "completed_at",
]);
// Finding 只公开确定性分类和有界位置序号，命中文本、坐标、XML path 与对象号没有字段。
const FINDING_KEYS = Object.freeze([
  "finding_id",
  "detector_code",
  "category",
  "severity",
  "confidence_milli",
  "source_kind",
  "location_kind",
  "location_index",
  "status",
  "created_at",
]);

// detector code 与 category 固定映射，防止未知规则通过通用字符串进入 DOM 或改变风险语义。
const DETECTOR_CATEGORIES = Object.freeze({
  "prompt.visible_instruction": "prompt_injection",
  "visual.hidden_text": "visual_hiding",
  "visual.white_text": "visual_hiding",
  "visual.tiny_text": "visual_hiding",
  "visual.hidden_layer": "hidden_layer",
  "ocr.prompt_instruction": "ocr",
  "ocr.text_observed": "ocr",
  "container.nested_archive": "container",
  "coverage.incomplete": "coverage",
});
// 严重度序号只用于报告内反向汇总最高风险，不能作为可配置阈值。
const SEVERITY_RANK = Object.freeze({ info: 0, low: 1, medium: 2, high: 3, critical: 4 });
// 报告终态固定为成功、阻断或无法形成可信 finding 的失败。
const REPORT_STATUSES = new Set(["passed", "blocked", "failed"]);
// unknown 只属于 failed 报告，组合限制由 validReportState 执行。
const RISK_LEVELS = new Set(["none", "low", "medium", "high", "critical", "unknown"]);
// 覆盖状态不能使用布尔值，以免 partial 被误解释为完整。
const COVERAGE_STATUSES = new Set(["complete", "partial", "unavailable"]);
// OCR 状态区分无需执行、部分覆盖、依赖不可用和真实执行失败。
const OCR_STATUSES = new Set(["not_required", "completed", "partial", "unavailable", "failed"]);
// 来源只允许不泄露路径、坐标或正文的固定结构枚举。
const SOURCE_KINDS = new Set([
  "visible_text",
  "docx_xml",
  "docx_image",
  "pdf_content",
  "pdf_image",
  "document_container",
  "scan_coverage",
]);
// 位置类型只配合有界整数序号使用，不能承载任意 part 名或对象路径。
const LOCATION_KINDS = new Set(["document", "page", "part", "image"]);
// OCR 版本与离线部署包冻结，未知版本响应不得进入页面。
const OCR_ENGINE_VERSION = "rapidocr-onnxruntime-1.4.4";
// 256 条 finding 的固定 CSV 远小于该硬上限，防止代理异常放大浏览器内存。
const MAX_CSV_BYTES = 2 * 1024 * 1024;

// 页面文案只由统一错误码决定，后端自由 message 或第三方解析器异常一律不展示。
const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有文件安全操作权限",
  "infra.validation_failed": "文件安全请求格式无效",
  "file.not_found": "当前租户中找不到该文件",
  "file.security_report_not_found": "该文件尚未生成安全报告",
  "file.security_not_ready": "文件尚未达到安全扫描条件",
  "file.security_scan_busy": "安全扫描任务繁忙，请稍后重试",
  "file.security_scan_unavailable": "文件安全扫描暂不可用",
  "file.model_access_blocked": "文件未通过模型上下文准入",
  "file.integrity_failed": "文件安全事实完整性校验失败",
  "file.security_response_invalid": "文件安全响应合同不完整",
  "file.security_export_invalid": "文件安全报告导出响应无效",
});

/**
 * 读取 PAGE-103 可选择的 ready 文件。
 *
 * @param {object} options Control 地址、Session Token、私有 cursor 与 AbortSignal。
 * @returns {Promise<object>} 复用 PRD-072 strict FileInfo 的每页最多 25 条 ready 文件列表。
 *
 * tenant、actor 和排序不接受调用方覆盖；后端仍以 Session tenant 作为权威 owner。
 */
export async function listFileSecurityFiles({ controlApiUrl, token, cursor = null, signal }) {
  const result = await listFiles({
    controlApiUrl,
    token,
    status: "ready",
    limit: 25,
    cursor,
    signal,
  });
  // 即使服务端忽略筛选，PAGE-103 也不能让非 ready 文件成为扫描候选。
  if (result.files.some((file) => file.status !== "ready" || file.extract?.status !== "ready")) {
    throw fileSecurityClientError("file.security_response_invalid", 502, result.request_id, result.trace_id);
  }
  return result;
}

/**
 * 读取当前 detector version 的文件安全报告。
 *
 * @param {object} options Control 地址、Token、规范 file ID 与 AbortSignal。
 * @returns {Promise<object>} exact-key 校验并深度冻结的报告信封。
 */
export async function getFileSecurityReport({ controlApiUrl, token, fileId, signal }) {
  const normalizedFileId = requireFileId(fileId);
  const payload = await requestJson(
    `${controlApiUrl}/files/${encodeURIComponent(normalizedFileId)}/security-report`,
    { headers: authorizationHeaders(token), signal },
  );
  const normalized = normalizeFileSecurityReportResponse(payload, normalizedFileId);
  if (!normalized) {
    // HTTP 成功但 exact-set 漂移仍失败关闭，不能返回部分可信报告。
    throw fileSecurityClientError("file.security_response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

/**
 * 对当前 ready 文件执行一次安全扫描，或幂等复用现有权威报告。
 *
 * @param {object} options Control 地址、Token、规范 file ID 与 AbortSignal。
 * @returns {Promise<object>} 与 GET 完全相同的严格报告信封。
 *
 * POST 不发送 body、Content-Type、tenant 或 detector 配置，关闭请求级规则和阈值注入入口。
 */
export async function scanFileSecurity({ controlApiUrl, token, fileId, signal }) {
  const normalizedFileId = requireFileId(fileId);
  const payload = await requestJson(
    `${controlApiUrl}/files/${encodeURIComponent(normalizedFileId)}/security-scan`,
    { method: "POST", headers: authorizationHeaders(token), signal },
  );
  const normalized = normalizeFileSecurityReportResponse(payload, normalizedFileId);
  if (!normalized) {
    // 扫描成功响应必须与读取接口执行完全相同的严格投影。
    throw fileSecurityClientError("file.security_response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

/**
 * 下载与 JSON 同一 canonical 报告对应的 CSV。
 *
 * @param {object} options Control 地址、Token、规范 file ID 与 AbortSignal。
 * @returns {Promise<object>} 有界 Blob、可信固定文件名和响应链路 ID。
 *
 * Blob 只返回给当前点击调用栈，不应进入页面状态、Storage 或日志。
 */
export async function downloadFileSecurityReportCsv({ controlApiUrl, token, fileId, signal }) {
  const normalizedFileId = requireFileId(fileId);
  let response;
  try {
    response = await fetch(
      `${controlApiUrl}/files/${encodeURIComponent(normalizedFileId)}/security-report.csv`,
      {
        headers: { ...authorizationHeaders(token), Accept: "text/csv" },
        signal,
        cache: "no-store",
        credentials: "omit",
      },
    );
  } catch (error) {
    // owner 主动取消必须原样交给页面状态机静默丢弃。
    if (error?.name === "AbortError") throw error;
    // 原始网络异常可能携带代理 URL，只折叠为固定依赖错误。
    throw fileSecurityClientError("file.security_scan_unavailable", 503);
  }
  if (!response.ok) {
    const payload = await readJson(response);
    // CSV 错误仍使用统一 JSON 错误信封，服务端自由 message 不进入前端。
    throw responseError(response, payload);
  }
  const expectedFilename = `${normalizedFileId}-security-report.csv`;
  const contentType = String(response.headers.get("Content-Type") ?? "").toLowerCase();
  const disposition = String(response.headers.get("Content-Disposition") ?? "");
  const cacheControl = String(response.headers.get("Cache-Control") ?? "").toLowerCase();
  const nosniff = String(response.headers.get("X-Content-Type-Options") ?? "").toLowerCase();
  const requestId = safeChainId(response.headers.get("X-Request-ID"));
  const traceId = safeChainId(response.headers.get("X-Trace-ID"));
  if (
    !contentType.startsWith("text/csv")
    || disposition !== `attachment; filename="${expectedFilename}"`
    || !cacheControl.includes("no-store")
    || nosniff !== "nosniff"
    || !requestId
    || !traceId
  ) {
    // 任一安全 Header 或固定下载名漂移都拒绝读取 Blob。
    throw fileSecurityClientError("file.security_export_invalid", 502, requestId, traceId);
  }
  const blob = await response.blob();
  if (!(blob instanceof Blob) || blob.size < 1 || blob.size > MAX_CSV_BYTES) {
    // 空制品和异常放大响应都不能创建 Object URL。
    throw fileSecurityClientError("file.security_export_invalid", 502, requestId, traceId);
  }
  return Object.freeze({
    state: "ok",
    fileId: normalizedFileId,
    blob,
    filename: expectedFilename,
    requestId,
    traceId,
  });
}

/**
 * 使用一次性 Object URL 触发 CSV 下载，并在 click 成功或失败后立即 revoke。
 *
 * @param {object} result downloadFileSecurityReportCsv 返回值。
 * @param {object} environment 可测试的 URL/document 环境，默认使用当前浏览器。
 * @returns {void}
 */
export function triggerFileSecurityReportCsvDownload(result, environment = globalThis) {
  const urlApi = environment?.URL;
  const documentApi = environment?.document;
  const expectedFilename = `${result?.fileId ?? ""}-security-report.csv`;
  if (
    result?.state !== "ok"
    || !isFileId(result.fileId)
    || result.filename !== expectedFilename
    || !(result.blob instanceof Blob)
    || result.blob.size < 1
    || typeof urlApi?.createObjectURL !== "function"
    || typeof urlApi?.revokeObjectURL !== "function"
    || typeof documentApi?.createElement !== "function"
    || !documentApi.body
  ) {
    // 环境或结果合同不完整时不能创建一个无法保证回收的 Blob URL。
    throw fileSecurityClientError("file.security_export_invalid", 502);
  }
  const link = documentApi.createElement("a");
  const objectUrl = urlApi.createObjectURL(result.blob);
  try {
    link.href = objectUrl;
    link.download = expectedFilename;
    link.rel = "noopener";
    link.hidden = true;
    documentApi.body.append(link);
    link.click();
  } finally {
    // remove/revoke 放在 finally，浏览器或策略阻止 click 时也不能残留可访问 Blob URL。
    link.remove?.();
    urlApi.revokeObjectURL(objectUrl);
  }
}

/**
 * 校验报告信封的 exact-set、枚举、计数和三种终态组合。
 *
 * @param {object} payload 待校验 JSON。
 * @param {string} expectedFileId 路径 owner；响应必须与其一致。
 * @returns {object|null} 深度冻结的安全投影；任一合同漂移返回 null。
 */
export function normalizeFileSecurityReportResponse(payload, expectedFileId) {
  if (
    !hasExactKeys(payload, RESPONSE_KEYS)
    || typeof payload.reused !== "boolean"
    || !safeChainId(payload.request_id)
    || !safeChainId(payload.trace_id)
  ) return null;
  const report = normalizeReport(payload.report, expectedFileId);
  if (!report) return null;
  return Object.freeze({
    state: "ok",
    report,
    reused: payload.reused,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 深度校验报告头与 findings 之间的风险、计数、时间和模型准入关系。 */
function normalizeReport(value, expectedFileId) {
  if (!hasExactKeys(value, REPORT_KEYS) || !Array.isArray(value.findings)) return null;
  if (
    !/^fscn_[0-9a-f]{32}$/.test(String(value.scan_id ?? ""))
    || !isFileId(value.file_id)
    || (expectedFileId !== undefined && value.file_id !== expectedFileId)
    || !REPORT_STATUSES.has(value.status)
    || !RISK_LEVELS.has(value.risk_level)
    || !nullableInteger(value.risk_score, 0, 100)
    || !COVERAGE_STATUSES.has(value.coverage_status)
    || !["allowed", "blocked"].includes(value.model_access_status)
    || value.detector_version !== "file-security-detector-v1"
    || value.boundary_version !== "file-context-boundary-v1"
    || !OCR_STATUSES.has(value.ocr_status)
    || !(value.ocr_engine_version === null || value.ocr_engine_version === OCR_ENGINE_VERSION)
    || !safeInteger(value.scanned_page_count, 0, 10_000)
    || !safeInteger(value.scanned_image_count, 0, 10_000)
    || !safeInteger(value.finding_count, 0, 256)
    || !safeInteger(value.high_finding_count, 0, value.finding_count)
    || value.finding_count !== value.findings.length
    || !canonicalUtc(value.created_at)
    || value.completed_at !== value.created_at
  ) return null;
  // OCR 确实执行过时必须公开固定引擎版本；纯文本和明确不可用路径不能伪造已加载引擎。
  if (["completed", "partial"].includes(value.ocr_status) && value.ocr_engine_version !== OCR_ENGINE_VERSION) return null;
  if (value.ocr_status === "not_required" && value.ocr_engine_version !== null) return null;
  const findings = value.findings.map((item) => normalizeFinding(item, value.created_at));
  if (findings.some((item) => !item)) return null;
  const findingIds = findings.map((item) => item.finding_id);
  if (new Set(findingIds).size !== findingIds.length) return null;
  const highCount = findings.filter((item) => ["high", "critical"].includes(item.severity)).length;
  if (highCount !== value.high_finding_count) return null;
  const highestRisk = findings.reduce((highest, item) => {
    const candidate = item.severity === "info" ? "low" : item.severity;
    return (SEVERITY_RANK[candidate] ?? 0) > (SEVERITY_RANK[highest] ?? 0) ? candidate : highest;
  }, "none");
  if (value.status !== "failed" && value.risk_level !== highestRisk) return null;
  if (!validReportState(value)) return null;
  return Object.freeze({
    scan_id: value.scan_id,
    file_id: value.file_id,
    status: value.status,
    risk_level: value.risk_level,
    risk_score: value.risk_score,
    coverage_status: value.coverage_status,
    model_access_status: value.model_access_status,
    detector_version: value.detector_version,
    boundary_version: value.boundary_version,
    ocr_status: value.ocr_status,
    ocr_engine_version: value.ocr_engine_version,
    scanned_page_count: value.scanned_page_count,
    scanned_image_count: value.scanned_image_count,
    finding_count: value.finding_count,
    high_finding_count: value.high_finding_count,
    findings: Object.freeze(findings),
    created_at: value.created_at,
    completed_at: value.completed_at,
  });
}

/** 校验一条 finding；location 只允许 document/null 或有界正整数位置。 */
function normalizeFinding(value, reportCreatedAt) {
  if (!hasExactKeys(value, FINDING_KEYS)) return null;
  const expectedCategory = DETECTOR_CATEGORIES[value.detector_code];
  if (
    !/^fsfd_[0-9a-f]{32}$/.test(String(value.finding_id ?? ""))
    || !expectedCategory
    || value.category !== expectedCategory
    || !Object.hasOwn(SEVERITY_RANK, value.severity)
    || !safeInteger(value.confidence_milli, 0, 1000)
    || !SOURCE_KINDS.has(value.source_kind)
    || !LOCATION_KINDS.has(value.location_kind)
    || value.status !== "detected"
    || !canonicalUtc(value.created_at)
    || value.created_at !== reportCreatedAt
  ) return null;
  const documentLocation = value.location_kind === "document";
  if (documentLocation !== (value.location_index === null)) return null;
  if (!documentLocation && !safeInteger(value.location_index, 1, 100_000)) return null;
  if (
    value.detector_code === "coverage.incomplete"
    && !(value.severity === "high" && value.source_kind === "scan_coverage")
  ) return null;
  return Object.freeze({
    finding_id: value.finding_id,
    detector_code: value.detector_code,
    category: value.category,
    severity: value.severity,
    confidence_milli: value.confidence_milli,
    source_kind: value.source_kind,
    location_kind: value.location_kind,
    location_index: value.location_index,
    status: "detected",
    created_at: value.created_at,
  });
}

/** 冻结 failed/passed/blocked 的互斥状态，尤其禁止 failed 伪装成 0 分 clean。 */
function validReportState(report) {
  if (report.status === "failed") {
    return report.risk_level === "unknown"
      && report.risk_score === null
      && report.coverage_status === "unavailable"
      && report.model_access_status === "blocked"
      && report.finding_count === 0
      && report.high_finding_count === 0
      && ["unavailable", "failed"].includes(report.ocr_status);
  }
  if (!Number.isSafeInteger(report.risk_score)) return false;
  if (report.status === "passed") {
    return ["none", "low", "medium"].includes(report.risk_level)
      && report.coverage_status === "complete"
      && report.model_access_status === "allowed"
      && ["not_required", "completed"].includes(report.ocr_status);
  }
  return report.model_access_status === "blocked"
    && (
      ["high", "critical"].includes(report.risk_level)
      || ["partial", "unavailable"].includes(report.coverage_status)
    );
}

/** 统一执行 no-store JSON 请求，并把所有非 Abort 网络异常折叠为固定依赖错误。 */
async function requestJson(url, options) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      cache: "no-store",
      credentials: "omit",
      headers: options.headers ?? {},
    });
  } catch (error) {
    // 状态机主动 Abort 不转换为依赖故障，避免离页产生错误反馈。
    if (error?.name === "AbortError") throw error;
    // 非 Abort 网络异常一律不暴露 URL 或浏览器实现细节。
    throw fileSecurityClientError("file.security_scan_unavailable", 503);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    // 统一错误只保留固定 code 和链路 ID。
    throw responseError(response, payload);
  }
  return payload;
}

/** 把统一后端错误投影为只含固定 code、文案和链路 ID 的 AuthApiError。 */
function responseError(response, payload) {
  const code = safeErrorCode(payload?.code) || "file.security_scan_unavailable";
  return fileSecurityClientError(
    code,
    response.status,
    payload?.request_id || response.headers.get("X-Request-ID"),
    payload?.trace_id || response.headers.get("X-Trace-ID"),
  );
}

/** 认证 Header 只属于当前 fetch 调用栈，Token 不复制到页面状态或下载 URL。 */
function authorizationHeaders(token) {
  return { Accept: "application/json", Authorization: `Bearer ${token}` };
}

/** 解析 JSON 失败返回空对象，代理 HTML 与底层错误正文不能进入异常。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/** 创建不含服务端自由 message 的统一前端错误。 */
function fileSecurityClientError(code, status, requestId = "-", traceId = "-") {
  const normalizedCode = safeErrorCode(code) || "file.security_scan_unavailable";
  return new AuthApiError({
    status,
    code: normalizedCode,
    message: ERROR_MESSAGES[normalizedCode] ?? "文件安全服务暂不可用",
    requestId: safeChainId(requestId) || "-",
    traceId: safeChainId(traceId) || "-",
    suggestion: "",
    diagnosis: null,
  });
}

/** file ID 必须在出网前完成规范校验，禁止斜杠和编码变体参与路径解析。 */
function requireFileId(value) {
  const normalized = String(value ?? "").trim();
  if (!isFileId(normalized)) throw fileSecurityClientError("file.not_found", 404);
  return normalized;
}

/** 返回值是否为规范 file ID。 */
function isFileId(value) {
  return /^file_[0-9a-f]{32}$/.test(String(value ?? ""));
}

/** exact-key 校验拒绝数组、null 和未来未评审字段。 */
function hasExactKeys(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const keys = [...expected].sort();
  return actual.length === keys.length && actual.every((key, index) => key === keys[index]);
}

/** 只接受 UTC 秒精度 canonical 时间，避免等价多表示绕过报告一致性比较。 */
function canonicalUtc(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return false;
  const parsed = new Date(value);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().replace(".000Z", "Z") === value;
}

/** 统一验证闭区间安全整数。 */
function safeInteger(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/** risk_score 只有 failed 可以为 null，组合约束由 validReportState 再次验证。 */
function nullableInteger(value, minimum, maximum) {
  return value === null || safeInteger(value, minimum, maximum);
}

/** request/trace ID 最长 128 个安全 ASCII，与后端迁移字段保持一致。 */
function safeChainId(value) {
  const normalized = String(value ?? "").trim();
  return /^[A-Za-z0-9_.:-]{1,128}$/.test(normalized) ? normalized : "";
}

/** 统一错误码只允许固定点分命名字符。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}
