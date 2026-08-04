import { AuthApiError } from "./auth.js";

/**
 * 查询当前租户可见的预算规则列表。
 * 前端只传 scope/status 筛选条件，不传 tenant_id；租户隔离和角色校验必须由控制面预算 API 完成。
 */
export async function listBudgets({ controlApiUrl, token, scopeType, environmentId, status }) {
  const url = new URL(`${controlApiUrl}/budgets`);
  setOptionalParam(url, "scope_type", scopeType);
  setOptionalParam(url, "environment_id", environmentId);
  setOptionalParam(url, "status", status);
  return budgetRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 查询租户级和可选 Virtual Key 级的当日预算状态。
 * 该接口用于控制台概览和预算设置页，只返回成本摘要与策略动作，不返回任何 Key 明文。
 */
export async function getBudgetStatus({ controlApiUrl, token, virtualKeyId, environmentId, projectId, applicationId }) {
  const url = new URL(`${controlApiUrl}/budget-status`);
  setOptionalParam(url, "virtual_key_id", virtualKeyId);
  setOptionalParam(url, "environment_id", environmentId);
  setOptionalParam(url, "project_id", projectId);
  setOptionalParam(url, "application_id", applicationId);
  return budgetRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * create-only 创建预算规则。
 *
 * POST 与兼容 PATCH 共用同一安全字段白名单，但重复 Environment/scope 必须由后端返回
 * budget.rule_conflict，前端不能把 409 偷换为一次覆盖写入。
 */
export async function createBudgetRule(options) {
  return writeBudgetRule(options, "POST");
}

/**
 * 创建或更新单个预算规则。
 * payload 只包含用户显式填写的安全配置字段；空字符串会被省略，避免把空值误写入财务规则。
 */
export async function updateBudgetRule({
  controlApiUrl,
  token,
  scopeType,
  scopeId,
  environmentId,
  name,
  dailyBudgetCny,
  warningThresholdPercent,
  exhaustedAction,
  status,
}) {
  return writeBudgetRule({
    controlApiUrl,
    token,
    scopeType,
    scopeId,
    environmentId,
    name,
    dailyBudgetCny,
    warningThresholdPercent,
    exhaustedAction,
    status,
  }, "PATCH");
}

/**
 * 使用明确 HTTP 方法写入预算规则。
 *
 * 参数只包含预算配置；tenant_id、操作者和可信资源归属全部由 Control 登录态恢复。
 */
async function writeBudgetRule({
  controlApiUrl,
  token,
  scopeType,
  scopeId,
  environmentId,
  name,
  dailyBudgetCny,
  warningThresholdPercent,
  exhaustedAction,
  status,
}, method) {
  const payload = compactPayload({
    scope_type: scopeType,
    scope_id: scopeId,
    environment_id: environmentId,
    name,
    daily_budget_cny: dailyBudgetCny,
    warning_threshold_percent: warningThresholdPercent,
    exhausted_action: exhaustedAction,
    status,
  });
  return budgetRequest(`${controlApiUrl}/budgets`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

/**
 * 按预算规则 ID 切换耗尽模式。
 * 路径只携带规则 ID，tenant_id 和 admin 权限由控制面登录态恢复并校验。
 */
export async function updateBudgetMode({ controlApiUrl, token, budgetRuleId, mode }) {
  return budgetRequest(`${controlApiUrl}/budgets/${encodeURIComponent(budgetRuleId)}/mode`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ mode }),
  });
}

/**
 * 设置可选查询参数，空字符串不进入 URL，避免后端收到无意义筛选条件。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 压缩预算写入 payload，保留 0 这类有效数字，丢弃空字符串、null 和 undefined。
 */
function compactPayload(payload) {
  return Object.fromEntries(
    Object.entries(payload).filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== ""),
  );
}

/**
 * 调用控制面预算 API，并把统一错误响应转换成控制台可展示的 AuthApiError。
 * 错误对象只携带 code/request_id/trace_id 等排障元数据，不输出底层 SQL 或密钥相关内容。
 */
async function budgetRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_budgets_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 预算写入直接影响阻断策略，错误必须保留后端标准 code 和 request_id，方便审计排查。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `预算接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON 响应体，避免空响应体造成前端二次异常。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
