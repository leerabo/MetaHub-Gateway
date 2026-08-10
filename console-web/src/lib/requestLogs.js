import { AuthApiError } from "./auth.js";
import { normalizeResponseCacheRequestLogInfo } from "./responseCache.js";

/**
 * 查询当前登录用户可见的调用日志列表。
 * 只把过滤条件放进 query string，不在前端拼接 tenant_id，租户隔离必须由后端鉴权上下文完成。
 */
export async function listRequestLogs({
  controlApiUrl,
  token,
  advancedFilters,
  savedFilterId,
  sortBy = "created_at",
  sortDirection = "desc",
  status,
  requestId,
  model,
  providerCode,
  environmentId,
  snapshotVersion,
  projectId,
  applicationId,
  errorCode,
  limit = 20,
  offset = 0,
  signal,
}) {
  const url = new URL(`${controlApiUrl}/request-logs`);
  const normalizedSavedFilterId = String(savedFilterId ?? "").trim();
  const normalizedAdvanced = compactAdvancedFilters(advancedFilters);
  // SavedFilter 必须由后端按当前 tenant + owner 重新加载权威筛选和排序，不能同时发送客户端副本。
  if (normalizedSavedFilterId) {
    url.searchParams.set("saved_filter_id", normalizedSavedFilterId);
  } else if (Object.keys(normalizedAdvanced).length) {
    url.searchParams.set("filters", encodeAdvancedFilters(normalizedAdvanced));
  }
  const usesStructuredFilterSource = normalizedSavedFilterId || Object.keys(normalizedAdvanced).length;
  if (!usesStructuredFilterSource) {
    // 高级筛选、SavedFilter 与 legacy 业务参数在后端是三种互斥来源；只在兼容模式追加旧参数。
    setOptionalParam(url, "status", status);
    setOptionalParam(url, "request_id", requestId);
    setOptionalParam(url, "model", model);
    setOptionalParam(url, "provider_code", providerCode);
    setOptionalParam(url, "environment_id", environmentId);
    // 版本值按原始字符串发送，由后端统一执行正整数、上界和 tenant_id 过滤校验，前端不伪造 v0。
    setOptionalParam(url, "snapshot_version", snapshotVersion);
    setOptionalParam(url, "project_id", projectId);
    setOptionalParam(url, "application_id", applicationId);
    setOptionalParam(url, "error_code", errorCode);
  }
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  // 应用 SavedFilter 时排序同样来自后端持久对象，避免旧页面覆盖服务器新版本。
  if (!normalizedSavedFilterId) {
    url.searchParams.set("sort_by", String(sortBy));
    url.searchParams.set("sort_direction", String(sortDirection));
  }
  // 该分支影响安全或治理链路，需要用中文说明处理边界。
  return requestLogsRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
}

/**
 * 列出当前登录用户自己的保存筛选。
 * owner 和 tenant 均由后端 Session 决定，客户端不发送可伪造的归属字段。
 */
export async function listSavedRequestLogFilters({ controlApiUrl, token, signal }) {
  return requestLogsRequest(`${controlApiUrl}/request-logs/saved-filters`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
}

/** 创建当前用户的保存筛选；分页位置永不进入持久化对象。 */
export async function createSavedRequestLogFilter({ controlApiUrl, token, name, filters, sortBy, sortDirection, signal }) {
  return requestLogsRequest(`${controlApiUrl}/request-logs/saved-filters`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(savedFilterBody({ name, filters, sortBy, sortDirection })),
    signal,
  });
}

/** 使用乐观版本更新当前用户自己的保存筛选，防止多标签页静默覆盖。 */
export async function updateSavedRequestLogFilter({ controlApiUrl, token, savedFilterId, version, name, filters, sortBy, sortDirection, signal }) {
  return requestLogsRequest(`${controlApiUrl}/request-logs/saved-filters/${encodeURIComponent(savedFilterId)}`, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ version, ...savedFilterBody({ name, filters, sortBy, sortDirection }) }),
    signal,
  });
}

/** 软删除当前用户自己的保存筛选；version 作为并发保护，不允许无条件删除。 */
export async function deleteSavedRequestLogFilter({ controlApiUrl, token, savedFilterId, version, signal }) {
  const url = new URL(`${controlApiUrl}/request-logs/saved-filters/${encodeURIComponent(savedFilterId)}`);
  url.searchParams.set("version", String(version));
  return requestLogsRequest(url.toString(), {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
    signal,
    allowEmpty: true,
  });
}

/** 把 UI camelCase 白名单映射为服务端 v1 筛选对象，并剔除空值和分页/排序字段。 */
export function compactAdvancedFilters(value) {
  const source = value && typeof value === "object" ? value : {};
  const mapping = {
    requestId: "request_id", traceId: "trace_id", status: "status",
    requestModel: "request_model", resolvedModel: "resolved_model",
    providerCode: "provider_code", virtualKeyId: "virtual_key_id",
    environmentId: "environment_id", projectId: "project_id",
    applicationId: "application_id", snapshotVersion: "snapshot_version",
    errorCode: "error_code", createdFrom: "created_from", createdTo: "created_to",
    latencyMsMin: "latency_ms_min", latencyMsMax: "latency_ms_max",
    totalTokensMin: "total_tokens_min", totalTokensMax: "total_tokens_max",
    costAmountMin: "cost_amount_min", costAmountMax: "cost_amount_max", currency: "currency",
    terminalStage: "terminal_stage",
    outputSafetyStatus: "output_safety_status",
    outputSafetyMode: "output_safety_mode",
    outputSafetyExposure: "output_safety_exposure",
    outputSafetyBilling: "output_safety_billing",
  };
  const result = {};
  for (const [clientName, serverName] of Object.entries(mapping)) {
    const raw = source[clientName] ?? source[serverName];
    if (raw !== undefined && raw !== null && String(raw).trim() !== "") {
      result[serverName] = typeof raw === "string" ? raw.trim() : raw;
    }
  }
  return result;
}

/** 生成无填充 Base64URL；canonical key 顺序由白名单映射固定，避免同一筛选产生不同指纹。 */
export function encodeAdvancedFilters(value) {
  const json = JSON.stringify({ v: 1, ...compactAdvancedFilters(value) });
  const bytes = new TextEncoder().encode(json);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

/** 组装保存筛选 body；客户端不携带 tenant_id、owner_user_id、limit 或 offset。 */
function savedFilterBody({ name, filters, sortBy, sortDirection }) {
  return {
    name: String(name ?? "").trim(),
    filters: { v: 1, ...compactAdvancedFilters(filters) },
    sort_by: String(sortBy ?? "created_at"),
    sort_direction: String(sortDirection ?? "desc"),
  };
}

/**
 * 查询单条调用日志详情。
 * request_id 来自服务端日志列表或用户输入，encodeURIComponent 防止路径注入。
 */
export async function getRequestLog({ controlApiUrl, token, requestId, signal }) {
  const payload = await requestLogsRequest(`${controlApiUrl}/request-logs/${encodeURIComponent(requestId)}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  const tenantId = String(payload?.request_log?.log?.tenant_id ?? "");
  // PRD-076 起 nullable 字段本身也属于 exact 合同；缺字段不能与明确 null 的历史/不适用事实混同。
  if (!payload?.request_log || !Object.hasOwn(payload.request_log, "response_cache")) {
    throw new AuthApiError({
      status: 502,
      code: "response_cache.response_invalid",
      message: "响应缓存响应合同不完整",
      requestId: payload?.request_id ?? "-",
      traceId: payload?.trace_id ?? "-",
    });
  }
  const responseCache = normalizeResponseCacheRequestLogInfo(
    payload.request_log.response_cache,
    tenantId,
  );
  if (responseCache === undefined) {
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    throw new AuthApiError({
      status: 502,
      code: "response_cache.response_invalid",
      message: "响应缓存响应合同不完整",
      requestId: payload?.request_id ?? "-",
      traceId: payload?.trace_id ?? "-",
    });
  }
  return {
    ...payload,
    request_log: {
      ...payload.request_log,
      response_cache: responseCache,
    },
  };
}

/**
 * 设置可选查询参数，空字符串不发送，避免后端收到无意义过滤条件。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 调用控制面 Request Log API，并把统一错误结构转换成前端可展示的 AuthApiError。
 * 这里不读取或输出任何敏感头，只保留 request_id/trace_id 便于排障。
 */
async function requestLogsRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_request_logs_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = response.status === 204 && options.allowEmpty ? {} : await readJson(response);
  if (!response.ok) {
    // 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `调用日志接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON 响应体，避免异常响应为空时前端再次抛解析错误。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
