import { AuthApiError } from "./auth.js";

// 审计导出响应必须同时满足服务端冻结合同；任一头异常都按不可用失败关闭，不能保存代理错误页。
const AUDIT_EXPORT_ID_PATTERN = /^aex_[0-9a-f]{32}$/;
const AUDIT_EXPORT_HASH_PATTERN = /^[0-9a-f]{64}$/;
const AUDIT_EXPORT_FILE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,118}\.csv$/;
const SAFE_LINK_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;
const SAFE_ERROR_CODE_PATTERN = /^[a-z][a-z0-9_.]{1,127}$/;
const MAX_AUDIT_EXPORT_ROWS = 10_000;
const MAX_AUDIT_EXPORT_BYTES = 16 * 1024 * 1024;

/**
 * 查询当前认证管理员可见的管理操作审计日志。
 *
 * 安全边界：客户端不接受也不发送 tenant_id，租户范围只能由后端根据 Bearer Token
 * 恢复；空筛选不会进入查询字符串，避免空值与后端精确匹配语义发生歧义。
 */
export async function listAuditLogs({
  controlApiUrl,
  token,
  actorUserId,
  action,
  objectType,
  objectId,
  requestId,
  createdFrom,
  createdTo,
  limit = 20,
  offset = 0,
}) {
  const url = new URL(`${controlApiUrl}/audit-logs`);
  appendAuditFilters(url, {
    actorUserId,
    action,
    objectType,
    objectId,
    requestId,
    createdFrom,
    createdTo,
  });
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));

  return auditLogsRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 按当前 PAGE-084 筛选同步导出审计 CSV。
 *
 * 该 GET 会创建不可变 AuditExport 和审计事件，因此只能由显式点击调用，并由调用方为每次新操作
 * 提供独立 operationRequestId。参数签名故意不接收 tenant_id、limit、offset 或 columns，避免误传。
 */
export async function exportAuditLogs({
  controlApiUrl,
  token,
  actorUserId,
  action,
  objectType,
  objectId,
  requestId,
  createdFrom,
  createdTo,
  operationRequestId,
  signal,
}) {
  const url = new URL(`${controlApiUrl}/audit-logs/export`);
  appendAuditFilters(url, {
    actorUserId,
    action,
    objectType,
    objectId,
    requestId,
    createdFrom,
    createdTo,
  });
  const safeOperationRequestId = requireLinkId(
    operationRequestId || createAuditExportRequestId(),
    "audit_export.unavailable",
    "审计导出 operation request_id 无效",
  );
  const response = await fetch(url.toString(), {
    signal,
    headers: {
      Accept: "text/csv",
      Authorization: `Bearer ${token}`,
      // operation request_id 是 tenant+admin 范围的幂等键；筛选 request_id 仅位于 query，二者不得混用。
      "X-Request-ID": safeOperationRequestId,
    },
  });
  if (!response.ok) {
    // 非 2xx 响应统一经安全解析器收敛，只保留白名单错误码与链路标识，原始正文不得进入页面。
    throw await readAuditExportError(response, safeOperationRequestId);
  }
  if (response.status !== 200) {
    // fetch 会把未知 2xx 视为成功，但同步导出合同只允许 200 CSV。
    throw auditExportClientError(
      "audit_export.unavailable",
      "审计导出响应状态无效",
      response.status,
      safeOperationRequestId,
    );
  }
  return readAuditExportResponse(response, safeOperationRequestId);
}

/**
 * 为一次新的人工导出生成 operation request_id。
 *
 * 标识只含用途、时间与随机量，不编码 tenant、用户、筛选或文件信息；调用方在同一网络重试中可复用它。
 */
export function createAuditExportRequestId() {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 20)
    ?? Math.random().toString(36).slice(2, 22);
  return `console_audit_export_${Date.now()}_${random}`;
}

/**
 * 使用一次性 object URL 触发已经完成安全校验的 CSV 下载。
 *
 * environment 只用于 Node 单测注入；生产环境即使 click 抛错也会移除临时节点并立即 revoke URL，
 * 避免持有审计制品的 Blob URL 跨页面存活。
 */
export function triggerAuditCsvDownload(result, environment = globalThis) {
  const urlApi = environment.URL;
  const documentApi = environment.document;
  validateAuditDownloadResult(result);
  if (!urlApi?.createObjectURL || !urlApi?.revokeObjectURL || !documentApi?.createElement || !documentApi.body) {
    // 缺少 object URL 或临时节点清理能力时必须拒绝下载，避免审计 Blob 以不可控方式驻留。
    throw auditExportClientError(
      "audit_export.unavailable",
      "当前浏览器不支持安全审计 CSV 下载",
      0,
      result?.requestId,
      result?.traceId,
    );
  }
  const objectUrl = urlApi.createObjectURL(result.blob);
  const link = documentApi.createElement("a");
  link.href = objectUrl;
  link.download = result.filename;
  link.hidden = true;
  try {
    documentApi.body.append(link);
    link.click();
  } finally {
    // 浏览器扩展、保存对话框或 click 失败都不能让对象 URL 和临时 DOM 节点残留。
    link.remove?.();
    urlApi.revokeObjectURL(objectUrl);
  }
}

/**
 * 为列表与导出追加完全相同的七个允许筛选。
 * 租户始终由 Bearer Session 恢复；分页仅由列表函数在本 helper 之外显式追加。
 */
function appendAuditFilters(url, filters) {
  setOptionalParam(url, "actor_user_id", filters.actorUserId);
  setOptionalParam(url, "action", filters.action);
  setOptionalParam(url, "object_type", filters.objectType);
  setOptionalParam(url, "object_id", filters.objectId);
  setOptionalParam(url, "request_id", filters.requestId);
  setOptionalParam(url, "created_from", filters.createdFrom);
  setOptionalParam(url, "created_to", filters.createdTo);
}

/**
 * 只追加非空的精确筛选值；前后空白不属于合法 ID、动作码或时间条件。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 调用只读审计接口并转换统一错误结构。
 *
 * 审计错误只保留后端允许展示的 code、message、request_id 和 trace_id，不把响应头、
 * Token 或任意原始正文拼进页面反馈。
 */
async function auditLogsRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_audit_logs_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 非 2xx 必须转换为统一错误对象并保留链路 ID，禁止把原始响应正文渲染到页面。
    throw new AuthApiError({
      status: response.status,
      code: payload.code || "audit_log.query_failed",
      message: payload.message || `审计日志接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 校验同步导出的全部公开响应元数据，再读取非空且有界的 Blob。
 * Header 原文不会进入错误对象或 DOM；错误路径只保留经白名单验证的 operation request_id。
 */
async function readAuditExportResponse(response, operationRequestId) {
  const requestId = safeLinkId(response.headers.get("X-Request-ID"));
  const traceId = safeLinkId(response.headers.get("X-Trace-ID"));
  const contentType = String(response.headers.get("Content-Type") ?? "").trim().toLowerCase();
  if (!/^text\/csv\s*;\s*charset=utf-8$/.test(contentType)) {
    // 非冻结 Content-Type 可能是代理错误页或任意响应，必须在读取 Blob 前失败关闭。
    throw auditExportClientError(
      "audit_export.unavailable",
      "审计导出响应不是 UTF-8 CSV",
      response.status,
      requestId,
      traceId,
    );
  }
  const filename = parseAuditExportFilename(response.headers.get("Content-Disposition"));
  const auditExportId = String(response.headers.get("X-MetaHub-Audit-Export-ID") ?? "").trim();
  const rowCount = parseAuditExportRowCount(response.headers.get("X-MetaHub-Audit-Export-Row-Count"));
  const hash = parseAuditExportEtag(response.headers.get("ETag"));
  if (!filename || !AUDIT_EXPORT_ID_PATTERN.test(auditExportId) || rowCount === null || !hash || !requestId || !traceId) {
    // 任一元数据缺失或越界都会破坏制品追踪，不能用 operation ID 等本地值伪造成功响应。
    throw auditExportClientError(
      "audit_export.unavailable",
      "审计导出响应元数据无效",
      response.status,
      requestId || operationRequestId,
      traceId,
    );
  }
  const blob = await response.blob();
  if (!(blob instanceof Blob) || blob.size < 1 || blob.size > MAX_AUDIT_EXPORT_BYTES) {
    // 空文件或超限 Blob 不可验收且可能造成内存风险，不能继续创建 object URL。
    throw auditExportClientError(
      "audit_export.unavailable",
      "审计导出文件大小无效",
      response.status,
      requestId,
      traceId,
    );
  }
  return {
    blob,
    filename,
    auditExportId,
    contentSha256: hash,
    rowCount,
    requestId,
    traceId,
  };
}

/**
 * 只接受 attachment 加双引号 ASCII basename 的冻结格式。
 * 路径、控制字符、RFC 5987 filename*、非 CSV 扩展和超长名称都拒绝，绝不回显原始 header。
 */
function parseAuditExportFilename(contentDisposition) {
  const match = String(contentDisposition ?? "").match(/^attachment;\s*filename="([A-Za-z0-9._-]+\.csv)"$/i);
  const candidate = match?.[1] ?? "";
  return AUDIT_EXPORT_FILE_PATTERN.test(candidate) ? candidate : "";
}

/** 解析强 ETag；只接受带双引号的 64 位小写 SHA-256，不接受 weak 或大小写变体。 */
function parseAuditExportEtag(value) {
  const match = String(value ?? "").match(/^"([0-9a-f]{64})"$/);
  return match && AUDIT_EXPORT_HASH_PATTERN.test(match[1]) ? match[1] : "";
}

/** 解析规范十进制行数；拒绝符号、小数、前导零和 10000 以上结果。 */
function parseAuditExportRowCount(value) {
  const normalized = String(value ?? "").trim();
  if (!/^(0|[1-9][0-9]{0,4})$/.test(normalized)) {
    return null;
  }
  const parsed = Number(normalized);
  return Number.isSafeInteger(parsed) && parsed <= MAX_AUDIT_EXPORT_ROWS ? parsed : null;
}

/**
 * 下载前再次校验内存 DTO，防止其他调用方绕过 HTTP 响应校验直接传入路径或空 Blob。
 */
function validateAuditDownloadResult(result) {
  if (
    !(result?.blob instanceof Blob)
    || result.blob.size < 1
    || result.blob.size > MAX_AUDIT_EXPORT_BYTES
    || !AUDIT_EXPORT_FILE_PATTERN.test(String(result.filename ?? ""))
    || !AUDIT_EXPORT_ID_PATTERN.test(String(result.auditExportId ?? ""))
    || !AUDIT_EXPORT_HASH_PATTERN.test(String(result.contentSha256 ?? ""))
    || !Number.isSafeInteger(result.rowCount)
    || result.rowCount < 0
    || result.rowCount > MAX_AUDIT_EXPORT_ROWS
    || !safeLinkId(result.requestId)
    || !safeLinkId(result.traceId)
  ) {
    // 下载入口可能被其他模块直接调用，DTO 任一字段不合约都必须在触发浏览器保存前拒绝。
    throw auditExportClientError(
      "audit_export.unavailable",
      "审计导出下载数据无效",
      0,
      safeLinkId(result?.requestId) || "-",
      safeLinkId(result?.traceId) || "-",
    );
  }
}

/**
 * 把后端统一错误响应收敛为 AuthApiError。
 * message、header dump 和原始正文均不透传；页面只消费固定 code 与安全 request_id。
 */
async function readAuditExportError(response, operationRequestId) {
  const payload = await readJson(response);
  const code = SAFE_ERROR_CODE_PATTERN.test(String(payload.code ?? ""))
    ? payload.code
    : "audit_export.unavailable";
  const requestId = safeLinkId(payload.request_id)
    || safeLinkId(response.headers.get("X-Request-ID"))
    || operationRequestId;
  const traceId = safeLinkId(payload.trace_id)
    || safeLinkId(response.headers.get("X-Trace-ID"))
    || "-";
  return auditExportClientError(code, "审计日志导出失败", response.status, requestId, traceId);
}

/** 返回通过公开链路标识白名单的值；非法输入收敛为空且不会进入错误或页面。 */
function safeLinkId(value) {
  const normalized = String(value ?? "").trim();
  return SAFE_LINK_ID_PATTERN.test(normalized) ? normalized : "";
}

/** 要求调用方提供合法 operation ID，避免畸形幂等键到达有副作用 GET。 */
function requireLinkId(value, code, message) {
  const normalized = safeLinkId(value);
  if (!normalized) {
    // 有副作用 GET 的幂等键必须先通过公开标识白名单，禁止把畸形或敏感值发给后端。
    throw auditExportClientError(code, message, 0, "-");
  }
  return normalized;
}

/** 构造前端安全检测错误；只携带固定文案和白名单链路标识。 */
function auditExportClientError(code, message, status, requestId = "-", traceId = "-") {
  return new AuthApiError({
    status,
    code,
    message,
    requestId: safeLinkId(requestId) || "-",
    traceId: safeLinkId(traceId) || "-",
    suggestion: "",
    diagnosis: null,
  });
}

/**
 * 容错读取 JSON；网关异常返回空正文时仍由统一错误分支生成可追踪反馈。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
