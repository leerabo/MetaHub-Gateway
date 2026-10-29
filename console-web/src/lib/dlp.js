import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * 查询当前租户的 DLP 规则列表。
 * 前端只提交状态、类型、动作和检测方向筛选，不允许拼接 tenant_id；真实租户隔离和权限由控制面接口完成。
 */
export async function listDlpRules({ controlApiUrl, authentication, token, signal, status, ruleType, action, scanScope }) {
  const url = new URL(`${controlApiUrl}/dlp/rules`);
  setOptionalParam(url, "status", status);
  setOptionalParam(url, "rule_type", ruleType);
  setOptionalParam(url, "action", action);
  setOptionalParam(url, "scan_scope", scanScope);
  return dlpRequest(url.toString(), {
    authentication, token, signal,
  });
}

/**
 * 创建当前租户的 DLP 规则。
 * pattern 只会提交给 admin 管理接口，不能写入 console 事件或普通调用日志；后端会对正则安全和租户归属再次校验。
 */
export async function createDlpRule({
  controlApiUrl,
  authentication,
  token,
  signal,
  name,
  ruleType,
  pattern,
  action,
  scanScope,
  status,
  priority,
  description,
}) {
  const payload = compactPayload({
    name,
    rule_type: ruleType,
    pattern,
    action,
    scan_scope: scanScope,
    status,
    priority,
    description,
  });
  return dlpRequest(`${controlApiUrl}/dlp/rules`, {
    authentication, token, signal,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

/**
 * 更新单条 DLP 规则的可编辑字段。
 * 路径 rule_id 来自后端列表，payload 不带 tenant_id；服务端仍会按登录态 tenant_id 查询，防止跨租户改规则。
 */
export async function updateDlpRule({
  controlApiUrl,
  authentication,
  token,
  signal,
  ruleId,
  name,
  pattern,
  action,
  scanScope,
  status,
  priority,
  description,
}) {
  const payload = compactPayload({
    name,
    pattern,
    action,
    scan_scope: scanScope,
    status,
    priority,
    description,
  });
  return dlpRequest(`${controlApiUrl}/dlp/rules/${encodeURIComponent(ruleId)}`, {
    authentication, token, signal,
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

/**
 * 设置可选 query 参数，空值不进入 URL，避免把无意义筛选条件传给后端。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 压缩写入 payload，保留数字 0 这类有效值，丢弃空字符串、null 和 undefined。
 */
function compactPayload(payload) {
  return Object.fromEntries(
    Object.entries(payload).filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== ""),
  );
}

/**
 * 调用控制面 DLP API，并把统一错误结构转换成控制台可展示的 AuthApiError。
 * 错误对象只携带 code/request_id/trace_id 等排障元数据，不输出规则 pattern、用户输入或 Provider Key。
 */
async function dlpRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_dlp_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // DLP 规则会影响请求是否进入上游，错误必须保留后端标准 code 和 request_id，便于安全负责人审计排查。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `DLP 接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON 响应体，避免空错误响应造成前端二次异常。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return {};
  }
}
