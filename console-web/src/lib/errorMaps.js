/**
 * 模块职责：封装 Provider 错误归一规则的租户内读取、创建和乐观锁更新请求。
 * 安全边界：tenant_id、actor、规则版本历史和原始上游错误均不得由浏览器提交；后端 RBAC
 * 与租户 owner 校验始终是最终权限边界。
 */

import { AuthApiError } from "./auth.js";

// request_id 只用于幂等和排障；随机值不编码租户、用户、Provider 或规则内容。
let errorMapRequestSequence = 0;

/** 生成一次规则写操作的安全 request_id；网络结果未知时由页面状态复用同一值。 */
export function createErrorMapRequestId() {
  const sequence = (++errorMapRequestSequence).toString(36);
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) {
    return `req_console_error_map_${uuid.replaceAll("-", "")}_${sequence}`;
  }
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
    return `req_console_error_map_${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}_${sequence}`;
  }
  return `req_console_error_map_${Date.now().toString(36)}_${sequence}`;
}

/**
 * 查询当前登录租户可见的内置规则与租户覆盖规则。
 * 查询参数使用固定白名单；tenant_id 不进入 URL，避免浏览器选择或伪造数据 owner。
 */
export async function listErrorMaps({
  controlApiUrl,
  token,
  errorMapId = "",
  providerId = "",
  source = "",
  status = "",
  outputErrorCode = "",
  limit = 100,
  offset = 0,
  signal,
  requestId = createErrorMapRequestId(),
}) {
  const url = new URL(`${controlApiUrl}/error-maps`);
  const exactErrorMapId = String(errorMapId ?? "").trim();
  // 冲突重取使用唯一资源 ID 并排除筛选分页，租户归属仍由认证 Session 后端校验。
  if (exactErrorMapId) {
    // 409 后精确重取与全部筛选、分页互斥，避免目标规则因旧页面条件再次遗漏。
    url.searchParams.set("error_map_id", exactErrorMapId);
  } else {
    setOptionalParam(url, "provider_id", providerId);
    setOptionalParam(url, "source", source);
    setOptionalParam(url, "status", status);
    setOptionalParam(url, "output_error_code", outputErrorCode);
    url.searchParams.set("limit", String(limit));
    url.searchParams.set("offset", String(offset));
  }
  return errorMapRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  }, requestId);
}

/**
 * 创建当前租户规则。
 * body 只复制可编辑字段，任何意外出现的 tenant_id、actor、hash 或版本字段都会被丢弃。
 */
export async function createErrorMap({
  controlApiUrl,
  token,
  draft,
  requestId = createErrorMapRequestId(),
  signal,
}) {
  return errorMapRequest(`${controlApiUrl}/error-maps`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(editableRuleBody(draft)),
    signal,
  }, requestId);
}

/**
 * 使用资源路径和 expected_version 更新当前租户规则。
 * error_map_id 只进入经过 encodeURIComponent 的 path，不在 body 重复提交，防止两个 owner 漂移。
 */
export async function updateErrorMap({
  controlApiUrl,
  token,
  errorMapId,
  expectedVersion,
  draft,
  requestId = createErrorMapRequestId(),
  signal,
}) {
  return errorMapRequest(
    `${controlApiUrl}/error-maps/${encodeURIComponent(String(errorMapId ?? ""))}`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        expected_version: expectedVersion,
        ...editableRuleBody(draft),
      }),
      signal,
    },
    requestId,
  );
}

/** 只复制 PRD-056 允许编辑的稳定字段，undefined 字段不进入 JSON intent。 */
export function editableRuleBody(value) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const fields = [
    "provider_id",
    "priority",
    "http_statuses",
    "http_status_class",
    "error_field",
    "field_operator",
    "field_pattern",
    "message_operator",
    "message_pattern",
    "output_error_code",
    "status",
  ];
  return Object.fromEntries(fields
    .filter((field) => source[field] !== undefined)
    .map((field) => [field, source[field]]));
}

/** 仅为非空值生成查询参数，所有值最终仍由后端执行格式和租户归属校验。 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 发送错误规则请求并转换统一错误。
 * 任一已解析 HTTP 响应都会携带 status；调用方据此终结 mutation request_id，只有 fetch
 * 直接抛出的网络结果未知错误才有资格复用旧 ID。
 */
async function errorMapRequest(url, options, requestId) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": requestId,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 所有已解析 HTTP 失败都封装统一 request_id，禁止向页面透传响应正文或底层网络细节。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `错误规则接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || requestId || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/** 空响应或代理非 JSON 错误稳定收敛为空对象，不读取或显示原始响应文本。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
