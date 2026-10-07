/**
 * 模块职责：封装策略管理列表、校验、创建和更新请求，并统一转换控制面错误。
 * 安全边界：租户、操作者、编译结果和版本归属均由后端恢复，客户端不得提交 tenant_id、AST 或 hash。
 */

import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * 查询当前认证租户的策略列表。
 *
 * 参数中的 status/action 只用于受限枚举筛选；signal 用于离页或刷新时取消旧请求。
 * 返回后端策略数组及 request_id/trace_id，真实租户隔离和 admin 权限由控制面执行。
 */
export async function listPolicies({ controlApiUrl, authentication, token, status = "all", action, signal }) {
  const url = new URL(`${controlApiUrl}/policies`);
  setOptionalParam(url, "status", status);
  setOptionalParam(url, "action", action === "all" ? undefined : action);
  return policyRequest(url.toString(), {
    authentication,
    token,
    signal,
  });
}

/**
 * 调用后端编译并校验单条策略草稿。
 *
 * conditionExpr/actionType/targetModelId 只形成受限 rule/action 对象；可选 context 供后续
 * Dry Run 使用，当前策略编辑器不会提交 tenant 字段或自行执行表达式。
 */
export async function validatePolicy({
  controlApiUrl,
  authentication,
  token,
  conditionExpr,
  actionType,
  targetModelId,
  context,
  signal,
}) {
  const payload = {
    rule: { condition_expr: String(conditionExpr ?? "") },
    action: buildActionPayload(actionType, targetModelId),
  };
  if (context !== undefined) {
    payload.context = context;
  }
  return policyRequest(`${controlApiUrl}/policies/validate`, {
    authentication,
    token,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal,
  });
}

/**
 * 创建当前认证租户的一条策略。
 *
 * 参数只覆盖管理员可编辑字段，tenant、对象 ID、版本、AST、hash 和 actor 均不得由浏览器伪造。
 */
export async function createPolicy({
  controlApiUrl,
  authentication,
  token,
  name,
  description,
  status,
  priority,
  conditionExpr,
  actionType,
  targetModelId,
  signal,
}) {
  return policyRequest(`${controlApiUrl}/policies`, {
    authentication,
    token,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildPolicyPayload({
      name,
      description,
      status,
      priority,
      conditionExpr,
      actionType,
      targetModelId,
    })),
    signal,
  });
}

/**
 * 使用 If-Match 乐观锁更新当前租户策略。
 *
 * version 只进入请求头而不进入 JSON；后端会在同一租户写事务中重新读取并比较最新版本。
 */
export async function updatePolicy({
  controlApiUrl,
  authentication,
  token,
  policyId,
  version,
  name,
  description,
  status,
  priority,
  conditionExpr,
  actionType,
  targetModelId,
  signal,
}) {
  return policyRequest(`${controlApiUrl}/policies/${encodeURIComponent(policyId)}`, {
    authentication,
    token,
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      "If-Match": `"${String(version)}"`,
    },
    body: JSON.stringify(buildPolicyPayload({
      name,
      description,
      status,
      priority,
      conditionExpr,
      actionType,
      targetModelId,
    })),
    signal,
  });
}

/**
 * 生成后端 Policy create/PATCH 的稳定嵌套结构。
 * 空描述显式转换成 null；非 route_to 动作的目标由动作构造器强制清空，避免残留模型 ID。
 */
function buildPolicyPayload({
  name,
  description,
  status,
  priority,
  conditionExpr,
  actionType,
  targetModelId,
}) {
  const normalizedDescription = String(description ?? "").trim();
  return {
    name: String(name ?? "").trim(),
    description: normalizedDescription || null,
    status: String(status ?? "active"),
    priority: Number(priority),
    rule: { condition_expr: String(conditionExpr ?? "") },
    action: buildActionPayload(actionType, targetModelId),
  };
}

/**
 * 构造单动作对象并落实 route_to 目标约束。
 * 前端清空无关目标只是减少误操作，目标是否 active、是否属于当前租户仍由后端权威校验。
 */
function buildActionPayload(actionType, targetModelId) {
  const type = String(actionType ?? "");
  const normalizedTarget = String(targetModelId ?? "").trim();
  return {
    type,
    target_model_id: type === "route_to" && normalizedTarget ? normalizedTarget : null,
  };
}

/**
 * 写入可选查询参数。
 * 空值不进入 URL，避免浏览器把 tenant_id 或无意义筛选通过通用对象意外透传。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 调用策略 API 并归一化响应与错误。
 * 错误只保留后端安全 message、code 和链路标识，不拼接草稿正文、AST 或底层异常。
 */
async function policyRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": createPolicyRequestId(),
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 策略错误必须保留统一 code/request_id 供审计排障，但不能回显表达式或模型归属细节。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `策略接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return {
    state: "ok",
    ...payload,
    etagVersion: readEtagVersion(response.headers.get("ETag")),
  };
}

/**
 * 生成浏览器侧策略请求 ID。
 * ID 只用于链路关联，不包含租户、用户、策略正文、Token 或 Provider Key。
 */
function createPolicyRequestId() {
  const randomId = globalThis.crypto?.randomUUID?.();
  return `console_policy_${randomId ?? Date.now()}`;
}

/**
 * 从标准 ETag 读取正整数版本。
 * 响应体仍是版本事实来源；该值只在代理保留 header 但响应字段缺失时辅助编辑器回填。
 */
function readEtagVersion(value) {
  const matched = String(value ?? "").match(/^"([1-9][0-9]*)"$/);
  return matched ? Number(matched[1]) : null;
}

/**
 * 容错读取 JSON 响应体。
 * 空响应或代理 HTML 错误会返回空对象，再由统一错误映射补齐稳定提示和响应头 request_id。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
