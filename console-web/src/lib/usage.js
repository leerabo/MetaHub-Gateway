import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

// 导出制品和任务状态使用固定白名单，避免服务端异常响应把任意路径、文件类型或状态带入浏览器流程。
const DEFAULT_USAGE_EXPORT_FILENAME = "metahub-usage-export.csv";
const USAGE_EXPORT_JOB_ID_PATTERN = /^uex_[A-Za-z0-9_-]{1,96}$/;
const USAGE_EXPORT_STATUSES = new Set(["pending", "processing", "completed", "failed", "expired"]);
const DEFAULT_EXPORT_POLL_ATTEMPTS = 60;
const DEFAULT_EXPORT_POLL_INTERVAL_MS = 2000;

/**
 * 查询当前租户的用量概览。
 * 前端只传筛选条件，不传 tenant_id；后端必须从登录态恢复租户边界并执行 RBAC 校验。
 */
export async function getUsageSummary({
  controlApiUrl,
  token,
  authentication,
  signal,
  consumerId,
  startDate,
  endDate,
  modelId,
  virtualKeyId,
  environmentId,
  projectId,
  applicationId,
  providerId,
  estimated,
  requestId,
  traceId,
}) {
  const url = new URL(`${controlApiUrl}/usage/summary`);
  appendUsageFilters(url, {
    consumerId,
    startDate,
    endDate,
    modelId,
    virtualKeyId,
    environmentId,
    projectId,
    applicationId,
    providerId,
    estimated,
    requestId,
    traceId,
  });
  // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
  return usageRequest(url.toString(), {
    authentication, token, signal,
  });
}

/**
 * 查询当前租户的用量明细列表。
 * 明细只用于成本排查和调用详情跳转，Provider Key、Virtual Key 明文和 Prompt 不应进入响应或页面。
 */
export async function listUsageRecords({
  controlApiUrl,
  token,
  authentication,
  signal,
  consumerId,
  startDate,
  endDate,
  modelId,
  virtualKeyId,
  environmentId,
  projectId,
  applicationId,
  providerId,
  estimated,
  requestId,
  traceId,
  limit = 20,
  offset = 0,
}) {
  const url = new URL(`${controlApiUrl}/usage/records`);
  appendUsageFilters(url, {
    consumerId,
    startDate,
    endDate,
    modelId,
    virtualKeyId,
    environmentId,
    projectId,
    applicationId,
    providerId,
    estimated,
    requestId,
    traceId,
  });
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
  return usageRequest(url.toString(), {
    authentication, token, signal,
  });
}

export async function getPersonalUsageSummary({
  controlApiUrl, token, authentication, signal, startDate, endDate,
}) {
  const url = new URL(`${controlApiUrl}/usage/me/summary`);
  appendPersonalUsageDates(url, startDate, endDate);
  return usageRequest(url.toString(), { authentication, token, signal });
}

export async function listPersonalUsageRecords({
  controlApiUrl, token, authentication, signal, startDate, endDate, limit = 20, offset = 0,
}) {
  const url = new URL(`${controlApiUrl}/usage/me/records`);
  appendPersonalUsageDates(url, startDate, endDate);
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  return usageRequest(url.toString(), { authentication, token, signal });
}

function appendPersonalUsageDates(url, startDate, endDate) {
  if (startDate) url.searchParams.set("start_date", String(startDate));
  if (endDate) url.searchParams.set("end_date", String(endDate));
}

/**
 * 按当前页面筛选显式创建 CSV 导出。
 * 参数只列出后端允许的 Usage 筛选；即使调用方对象中存在 tenant_id、limit 或 offset，也不会进入查询串。
 * 200 返回可下载 Blob，202 返回异步任务标识，二者都保留服务端 request_id/trace_id。
 */
export async function requestUsageCsvExport({
  controlApiUrl,
  token,
  authentication,
  consumerId,
  startDate,
  endDate,
  modelId,
  virtualKeyId,
  environmentId,
  projectId,
  applicationId,
  providerId,
  estimated,
  requestId,
  traceId,
  exportRequestId,
  exportTraceId,
  signal,
}) {
  const url = new URL(`${controlApiUrl}/usage/export.csv`);
  appendUsageFilters(url, {
    consumerId,
    startDate,
    endDate,
    modelId,
    virtualKeyId,
    environmentId,
    projectId,
    applicationId,
    providerId,
    estimated,
    requestId,
    traceId,
  });
  const response = await authenticatedResourceFetch(url.toString(), {
    authentication, token, csrfRequired: true,
    signal,
    headers: {
      Accept: "text/csv, application/json",
      // GET 导出会创建任务，独立请求标识是服务端 tenant_id + request_id 幂等键的一部分。
      "X-Request-ID": exportRequestId || createUsageExportRequestId(),
      // create、status 和 download 复用同一 trace，跨三段 HTTP 请求仍可在控制面日志中串联。
      "X-Trace-ID": exportTraceId || createUsageExportTraceId(),
    },
  });
  await throwUsageApiError(response, "用量导出接口请求失败");

  if (response.status === 202) {
    const payload = await readJson(response);
    const exportJob = normalizeUsageExportJob(payload.export_job ?? payload);
    return {
      kind: "pending",
      exportJob,
      usageExportJobId: exportJob.usage_export_job_id,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
    };
  }
  if (response.status !== 200) {
    // fetch 的 ok 会接受其他 2xx，但导出合同只允许 200 CSV 或 202 job，未知状态必须拒绝。
    throw usageClientError("usage.export_unavailable", "用量导出响应状态无效", response.status);
  }
  return readUsageCsvResponse(response);
}

/**
 * 查询当前租户异步导出任务状态。
 * URL 只由受校验的 job ID 和固定控制面根地址构造，不使用服务端返回的任意 URL，避免 Bearer Token 被带到其他源。
 */
export async function getUsageExportStatus({ controlApiUrl, token, authentication, usageExportJobId, traceId, signal }) {
  const jobId = requireUsageExportJobId(usageExportJobId);
  const response = await authenticatedResourceFetch(`${controlApiUrl}/usage/exports/${encodeURIComponent(jobId)}`, {
    authentication, token,
    signal,
    headers: {
      Accept: "application/json",
      "X-Request-ID": createUsageExportRequestId("status"),
      ...(traceId ? { "X-Trace-ID": traceId } : {}),
    },
  });
  await throwUsageApiError(response, "用量导出状态查询失败");
  const payload = await readJson(response);
  const exportJob = normalizeUsageExportJob(payload.export_job ?? payload);
  // 固定 URL 中的 job 与响应 DTO 必须一致，避免代理错配后继续下载另一个任务。
  if (exportJob.usage_export_job_id !== jobId) {
    throw usageClientError("usage.export_unavailable", "用量导出任务响应不一致", response.status);
  }
  return {
    exportJob,
    usageExportJobId: jobId,
    requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
    traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
  };
}

/**
 * 下载已经完成的异步 CSV 制品。
 * 下载继续携带 Bearer Token；job ID 只进入固定同源路径，Token、tenant_id 和筛选不会进入 URL。
 */
export async function downloadUsageExport({ controlApiUrl, token, authentication, usageExportJobId, traceId, signal }) {
  const jobId = requireUsageExportJobId(usageExportJobId);
  const response = await authenticatedResourceFetch(`${controlApiUrl}/usage/exports/${encodeURIComponent(jobId)}/download`, {
    authentication, token, csrfRequired: true,
    signal,
    headers: {
      Accept: "text/csv",
      "X-Request-ID": createUsageExportRequestId("download"),
      ...(traceId ? { "X-Trace-ID": traceId } : {}),
    },
  });
  await throwUsageApiError(response, "用量导出制品下载失败");
  return { ...(await readUsageCsvResponse(response)), usageExportJobId: jobId };
}

/**
 * 有限轮询异步任务并在完成后下载制品。
 * 最大次数和间隔可由单测注入；生产默认约等待两分钟，覆盖大结果生成同时仍保持有界。
 */
export async function pollUsageExportUntilReady({
  controlApiUrl,
  token,
  authentication,
  usageExportJobId,
  traceId,
  signal,
  maxAttempts = DEFAULT_EXPORT_POLL_ATTEMPTS,
  pollIntervalMs = DEFAULT_EXPORT_POLL_INTERVAL_MS,
  onStatus,
}) {
  const attempts = Math.max(1, Math.min(Number(maxAttempts) || 1, 120));
  const intervalMs = Math.max(0, Math.min(Number(pollIntervalMs) || 0, 10000));
  let lastResult = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    throwIfAborted(signal);
    lastResult = await getUsageExportStatus({ controlApiUrl, token, authentication, usageExportJobId, traceId, signal });
    onStatus?.(lastResult);
    const status = lastResult.exportJob.status;
    if (status === "completed") {
      // completed 仅代表制品可领取，实际下载仍需单独携带 Bearer 并接受后端二次权限校验。
      return downloadUsageExport({ controlApiUrl, token, authentication, usageExportJobId, traceId, signal });
    }
    if (status === "failed") {
      // 任务失败只展示后端固定 error_code，不读取 worker 内部异常或制品正文。
      throw usageClientError(
        lastResult.exportJob.error_code || "usage.export_unavailable",
        "用量导出任务生成失败",
        503,
        lastResult,
      );
    }
    if (status === "expired") {
      // 过期任务的 BLOB 已由后端清理，前端不得继续尝试下载或恢复旧 URL。
      throw usageClientError("usage.export_expired", "用量导出制品已过期", 410, lastResult);
    }
    if (attempt + 1 < attempts) {
      await abortableDelay(intervalMs, signal);
    }
  }
  // 达到有限次数后保留最后 request_id，让用户排查 worker 状态而不是无限轮询。
  throw usageClientError("usage.export_not_ready", "用量导出任务仍在处理中", 409, lastResult);
}

/**
 * 从 Content-Disposition 提取受信 CSV 文件名。
 * 只接受短 ASCII basename 和 .csv 扩展名，路径、控制字符、空白及其他扩展名统一回退固定名称。
 */
export function safeUsageCsvFilename(contentDisposition) {
  const match = String(contentDisposition ?? "").match(/filename="?([^";]+)"?/i);
  const candidate = match?.[1]?.trim() ?? "";
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.csv$/i.test(candidate)) {
    return DEFAULT_USAGE_EXPORT_FILENAME;
  }
  return candidate;
}

/**
 * 使用一次性对象 URL 触发 CSV 下载，并在成功或异常时回收 URL 与临时节点。
 * environment 参数仅供单测注入浏览器能力；生产调用使用 globalThis，不把 Blob 内容写入 DOM 或日志。
 */
export function triggerUsageCsvDownload(result, environment = globalThis) {
  const urlApi = environment.URL;
  const documentApi = environment.document;
  if (!urlApi?.createObjectURL || !urlApi?.revokeObjectURL || !documentApi?.createElement || !documentApi.body) {
    // 缺少任一生命周期能力都不能降级为裸链接，否则 Bearer 下载合同和 URL 回收会失效。
    throw usageClientError("usage.export_unavailable", "当前浏览器不支持安全 CSV 下载", 0, result);
  }
  const objectUrl = urlApi.createObjectURL(result.blob);
  const link = documentApi.createElement("a");
  link.href = objectUrl;
  link.download = safeUsageCsvFilename(`filename="${result.filename ?? ""}"`);
  link.hidden = true;
  try {
    documentApi.body.append(link);
    link.click();
  } finally {
    // 浏览器扩展或 click 异常也不能遗留持有财务制品的对象 URL。
    link.remove?.();
    urlApi.revokeObjectURL(objectUrl);
  }
}

/**
 * 为 Usage summary、records 和 export 追加完全相同的允许筛选。
 * 该白名单是前端防误传边界；真实租户隔离、未知参数拒绝和资源归属仍由后端执行。
 */
function appendUsageFilters(url, filters) {
  setOptionalParam(url, "consumer_id", filters.consumerId);
  setOptionalParam(url, "start_date", filters.startDate);
  setOptionalParam(url, "end_date", filters.endDate);
  setOptionalParam(url, "model_id", filters.modelId);
  setOptionalParam(url, "virtual_key_id", filters.virtualKeyId);
  setOptionalParam(url, "environment_id", filters.environmentId);
  setOptionalParam(url, "project_id", filters.projectId);
  setOptionalParam(url, "application_id", filters.applicationId);
  setOptionalParam(url, "provider_id", filters.providerId);
  setOptionalParam(url, "estimated", filters.estimated);
  setOptionalParam(url, "request_id", filters.requestId);
  setOptionalParam(url, "trace_id", filters.traceId);
}

/**
 * 设置可选查询参数，空字符串不发送，避免后端收到无意义筛选条件。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 调用控制面 Usage API，并把统一错误结构转换为控制台可展示的 AuthApiError。
 * 这里不读取或输出任何敏感头，只保留 request_id/trace_id 便于排障。
 */
async function usageRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_usage_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  await throwUsageApiError(response, `用量接口返回 ${response.status}`);
  const payload = await readJson(response);
  return { state: "ok", ...payload };
}

/**
 * 校验 CSV 成功响应并读取 Blob、文件名及链路标识。
 * Content-Type 不符合时拒绝下载，防止代理 HTML/JSON 错误页被保存为财务报表。
 */
async function readUsageCsvResponse(response) {
  const contentType = String(response.headers.get("Content-Type") ?? "").toLowerCase();
  if (!contentType.startsWith("text/csv")) {
    // 代理返回的 HTML/JSON 即使 HTTP 200 也不能伪装成财务 CSV 保存到本地。
    throw usageClientError(
      "usage.export_unavailable",
      "用量导出响应不是 CSV",
      response.status,
      {
        requestId: response.headers.get("X-Request-ID") || "-",
        traceId: response.headers.get("X-Trace-ID") || "-",
      },
    );
  }
  return {
    kind: "download",
    blob: await response.blob(),
    filename: safeUsageCsvFilename(response.headers.get("Content-Disposition")),
    requestId: response.headers.get("X-Request-ID") || "-",
    traceId: response.headers.get("X-Trace-ID") || "-",
  };
}

/**
 * 把 Usage API 非成功响应映射为统一 AuthApiError。
 * JSON 解析失败时只使用固定回退消息和响应头，不把 HTML、CSV 或代理正文带入页面。
 */
async function throwUsageApiError(response, fallbackMessage) {
  if (response.ok) {
    return;
  }
  const payload = await readJson(response);
  // 错误只保留统一公开字段和响应头链路，不读取或反射原始正文。
  throw new AuthApiError({
    status: response.status,
    code: payload.code || "usage.export_unavailable",
    message: payload.message || fallbackMessage,
    requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
    traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
    suggestion: payload.suggestion || "",
    diagnosis: payload.diagnosis ?? null,
  });
}

/**
 * 校验服务端任务 DTO 的最小公开合同，防止未知状态导致无限轮询或任意下载路径。
 */
function normalizeUsageExportJob(value) {
  const job = value && typeof value === "object" ? value : {};
  const jobId = requireUsageExportJobId(job.usage_export_job_id);
  const status = String(job.status ?? "");
  // 未知状态不能进入轮询分支，否则浏览器可能永远等待或错误领取制品。
  if (!USAGE_EXPORT_STATUSES.has(status)) {
    throw usageClientError("usage.export_unavailable", "用量导出任务状态无效", 502);
  }
  return { ...job, usage_export_job_id: jobId, status };
}

/**
 * 只接受服务端定义的 uex_ 标识；非法值不能进入 URL。
 */
function requireUsageExportJobId(value) {
  const normalized = String(value ?? "").trim();
  // 任务标识会进入受认证 URL，必须先限制为服务端 uex_ 安全字符集。
  if (!USAGE_EXPORT_JOB_ID_PATTERN.test(normalized)) {
    throw usageClientError("usage.export_unavailable", "用量导出任务标识无效", 502);
  }
  return normalized;
}

/**
 * 创建前端链路标识。随机部分只用于降低同毫秒碰撞，不承载租户、筛选或用户信息。
 */
export function createUsageExportRequestId(action = "create") {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 12)
    ?? Math.random().toString(36).slice(2, 14);
  return `console_usage_export_${action}_${Date.now()}_${random}`;
}

/**
 * 创建一次逻辑导出的稳定 trace_id。
 * 调用方在 create、status 和 download 之间复用该值，筛选、租户和用户信息都不会写入标识。
 */
export function createUsageExportTraceId() {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    ?? `${Date.now()}${Math.random().toString(36).slice(2, 14)}`;
  return `trc_console_usage_export_${random.slice(0, 48)}`;
}

/**
 * 构造前端检测到的固定 Usage 导出错误，并保留最近一次安全链路标识。
 */
function usageClientError(code, message, status, context = {}) {
  return new AuthApiError({
    status,
    code,
    message,
    requestId: context?.requestId ?? "-",
    traceId: context?.traceId ?? "-",
    suggestion: "",
    diagnosis: null,
  });
}

/**
 * 在轮询等待期间响应 AbortSignal；Abort 后立即清理监听器和定时器，不留下跨页面任务。
 */
function abortableDelay(delayMs, signal) {
  if (!delayMs) {
    throwIfAborted(signal);
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    function onAbort() {
      clearTimeout(timer);
      reject(abortError());
    }
    if (signal) {
      signal.addEventListener("abort", onAbort, { once: true });
    }
  });
}

/**
 * 在开始新一轮请求前同步检查取消状态，避免已经离页后仍发出一次状态查询。
 */
function throwIfAborted(signal) {
  if (signal?.aborted) {
    // 优先保留浏览器提供的标准 AbortError reason，调用方可把取消与业务失败分开处理。
    throw signal.reason instanceof Error ? signal.reason : abortError();
  }
}

/**
 * 创建浏览器和 Node 测试都可识别的 AbortError。
 */
function abortError() {
  if (typeof DOMException === "function") {
    return new DOMException("The operation was aborted.", "AbortError");
  }
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

/**
 * 容错读取 JSON 响应体，避免异常响应为空时前端再次抛解析错误。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return {};
  }
}
