import { AuthApiError } from "./auth.js";

// PAGE-083 只接受后端冻结的死信状态枚举；未知值必须在 fetch 前失败关闭。
const DEAD_LETTER_STATUSES = new Set([
  "all",
  "dead_letter",
  "replay_pending",
  "replayed",
  "archived",
  "expired",
]);

/**
 * 查询当前租户的 Webhook Endpoint 和最近发送记录。
 * 前端只提交状态和事件类型筛选，不拼接 tenant_id；真实租户隔离与角色校验由控制面 /webhooks 完成。
 */
export async function listWebhooks({ controlApiUrl, token, status, eventType, includeDeliveries = true }) {
  const url = new URL(`${controlApiUrl}/webhooks`);
  setOptionalParam(url, "status", status);
  setOptionalParam(url, "event_type", eventType);
  url.searchParams.set("include_deliveries", includeDeliveries ? "true" : "false");
  return webhookRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 创建当前租户的 Webhook Endpoint。
 * signing_secret 只会在创建响应中返回一次；前端不能把该明文写入 console 事件或普通列表状态。
 */
export async function createWebhook({ controlApiUrl, token, name, endpointUrl, eventTypes, status, timeoutMs }) {
  const payload = compactPayload({
    name,
    endpoint_url: endpointUrl,
    event_types: Array.isArray(eventTypes) ? eventTypes.filter(Boolean) : [],
    status,
    timeout_ms: timeoutMs,
  });
  return webhookRequest(`${controlApiUrl}/webhooks`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

/**
 * 触发单个 Endpoint 的测试事件。
 * 路径 ID 来自后端列表，后端仍会按登录态 tenant_id 重新确认归属并记录 attempts。
 */
export async function testWebhook({ controlApiUrl, token, webhookEndpointId }) {
  return webhookRequest(`${controlApiUrl}/webhooks/${encodeURIComponent(webhookEndpointId)}/test`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 查询单个 Endpoint 最近 100 条以内的发送记录。
 * 事件和状态筛选只作用于历史 delivery，不发送 tenant_id，也不依赖 Endpoint 当前订阅列表。
 */
export async function listWebhookDeliveries({
  controlApiUrl,
  token,
  webhookEndpointId,
  eventType,
  status = "all",
  limit = 100,
}) {
  const url = new URL(
    `${controlApiUrl}/webhooks/${encodeURIComponent(webhookEndpointId)}/deliveries`,
  );
  setOptionalParam(url, "event_type", eventType);
  setOptionalParam(url, "status", status);
  url.searchParams.set("limit", String(limit));
  return webhookRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 重放失败 delivery。
 * 幂等键由当前用户操作生成并只用于本次请求重试，前端不能提交事件正文、tenant_id 或操作者 ID。
 */
export async function replayWebhook({
  controlApiUrl,
  token,
  webhookEndpointId,
  webhookDeliveryId,
  idempotencyKey,
}) {
  return webhookRequest(`${controlApiUrl}/webhooks/${encodeURIComponent(webhookEndpointId)}/replay`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      webhook_delivery_id: webhookDeliveryId,
      idempotency_key: idempotencyKey,
    }),
  });
}

/**
 * 查询当前认证租户的 Webhook 死信安全摘要。
 *
 * 安全约束：签名故意不接受 tenant_id、actor、payload 或任意排序字段，真实租户只能由后端
 * 认证上下文恢复。cursor 只应驻留页面闭包，不得写入 URL hash 或浏览器存储。
 *
 * @param {object} options 冻结的查询参数与页面生命周期信号。
 * @returns {Promise<object>} 不含 payload、URL、Secret 或 lease 的死信列表响应。
 */
export async function listWebhookDeadLetters({
  controlApiUrl,
  token,
  status = "all",
  eventType,
  webhookEndpointId,
  limit = 50,
  cursor,
  signal,
  operationRequestId,
  operationTraceId,
}) {
  const normalizedStatus = String(status ?? "all").trim() || "all";
  // 本地先拒绝合同外状态与分页上限，避免无效筛选进入后端租户查询。
  if (!DEAD_LETTER_STATUSES.has(normalizedStatus) || !Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw localWebhookError("webhook.dead_letter_filter_invalid", "Webhook 死信筛选参数无效");
  }
  const url = new URL(`${controlApiUrl}/webhook-dead-letters`);
  url.searchParams.set("status", normalizedStatus);
  setOptionalParam(url, "event_type", eventType);
  setOptionalParam(url, "webhook_endpoint_id", webhookEndpointId);
  url.searchParams.set("limit", String(limit));
  setOptionalParam(url, "cursor", cursor);
  return webhookRequest(url.toString(), {
    cache: "no-store",
    headers: { Authorization: `Bearer ${token}` },
    signal,
    requestId: operationRequestId,
    traceId: operationTraceId,
  });
}

/**
 * 通过 Excel 指定的顶层 delivery 路径创建持久重放意图。
 *
 * 安全约束：请求体只能包含 idempotency_key；event_id、Endpoint、tenant、actor 和事件正文
 * 都由后端从来源 delivery 恢复，避免可篡改 DOM 扩大重放范围。
 *
 * @param {object} options 来源 delivery、内存幂等键和生命周期信号。
 * @returns {Promise<object>} 含安全 `dead_letter` 摘要的动作响应。
 */
export async function replayWebhookDelivery({
  controlApiUrl,
  token,
  webhookDeliveryId,
  idempotencyKey,
  signal,
  operationRequestId,
  operationTraceId,
}) {
  const deliveryId = requireOpaqueId(webhookDeliveryId, "webhook.dead_letter_not_found");
  const replayKey = String(idempotencyKey ?? "").trim();
  // 弱随机值不能承担持久重放幂等边界，格式不合格时禁止发送网络请求。
  if (!/^console_[A-Za-z0-9-]{16,96}$/.test(replayKey)) {
    throw localWebhookError("webhook.idempotency_key_invalid", "Webhook 重放幂等键无效");
  }
  return webhookRequest(
    `${controlApiUrl}/webhook-deliveries/${encodeURIComponent(deliveryId)}/replay`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ idempotency_key: replayKey }),
      signal,
      requestId: operationRequestId,
      traceId: operationTraceId,
    },
  );
}

/**
 * 逻辑归档当前租户的一条 Webhook 死信。
 *
 * 请求不发送 body，避免前端伪造 tenant、归档操作者、状态或归档时间；后端仍执行最终 RBAC、
 * tenant owner 和状态机校验。
 *
 * @param {object} options 死信 ID、认证信息和生命周期信号。
 * @returns {Promise<object>} 含安全 `dead_letter` 摘要的归档响应。
 */
export async function archiveWebhookDeadLetter({
  controlApiUrl,
  token,
  deadLetterEventId,
  signal,
  operationRequestId,
  operationTraceId,
}) {
  const eventId = requireOpaqueId(deadLetterEventId, "webhook.dead_letter_not_found");
  return webhookRequest(
    `${controlApiUrl}/webhook-dead-letters/${encodeURIComponent(eventId)}/archive`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      signal,
      requestId: operationRequestId,
      traceId: operationTraceId,
    },
  );
}

/**
 * 生成只驻留当前页面内存的重放幂等键。
 *
 * 浏览器支持 randomUUID 时优先使用；旧环境回退到 getRandomValues，仍不使用时间、租户或
 * delivery ID，避免幂等键泄露业务身份。两者均不可用时失败关闭，不生成弱随机键。
 *
 * @returns {string} `console_` 前缀的不透明随机键。
 */
export function createWebhookDeadLetterIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) {
    return `console_${globalThis.crypto.randomUUID()}`;
  }
  if (globalThis.crypto?.getRandomValues) {
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    return `console_${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
  }
  // 两种加密随机源都不可用时必须失败关闭，不能降级到时间戳或业务标识。
  throw localWebhookError("webhook.replay_unavailable", "当前浏览器无法安全生成重放幂等键");
}

/**
 * 判断失败是否可能发生在服务端已提交 intent 之后。
 *
 * @param {unknown} error 客户端捕获的网络或统一 API 错误。
 * @returns {boolean} 网络中断、408/429 和 5xx 返回 true，调用方必须复用原键重试。
 */
export function shouldRetainWebhookDeadLetterIdempotencyKey(error) {
  if (!(error instanceof AuthApiError)) {
    return true;
  }
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

/**
 * 原子轮换 Endpoint signing secret。
 * 请求不携带旧/新 secret；新明文只在本次响应返回，并由页面保存在短生命周期内存状态。
 */
export async function rotateWebhookSecret({ controlApiUrl, token, webhookEndpointId }) {
  return webhookRequest(`${controlApiUrl}/webhooks/${encodeURIComponent(webhookEndpointId)}/rotate-secret`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
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
 * 压缩 Webhook 写入 payload，保留数组和数字 0，丢弃空字符串、null 和 undefined。
 */
function compactPayload(payload) {
  return Object.fromEntries(
    Object.entries(payload).filter(([, value]) => {
      if (value === null || value === undefined) {
        return false;
      }
      if (Array.isArray(value)) {
        return value.length > 0;
      }
      return String(value).trim() !== "";
    }),
  );
}

/**
 * 调用控制面 Webhook API，并把统一错误响应转换成控制台可展示的 AuthApiError。
 * 错误对象只携带 code/request_id/trace_id，不输出 signing secret、签名头、请求 body 或接收端响应正文。
 */
async function webhookRequest(url, options = {}) {
  // requestId/traceId 是客户端控制字段，拆出后不能原样传给 fetch，避免形成未知 RequestInit 属性。
  const { requestId, traceId, ...fetchOptions } = options;
  const response = await fetch(url, {
    ...fetchOptions,
    headers: {
      "X-Request-ID": requestId || `console_webhooks_${Date.now()}`,
      ...(traceId ? { "X-Trace-ID": traceId } : {}),
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // Webhook 会把治理事件推送到外部系统，失败必须保留后端标准 code 和 request_id 便于排障。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `Webhook 接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON 响应体，避免空响应体导致前端二次异常。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/** 把本地失败关闭校验转换成与后端一致的公开错误对象，不包含调用参数原文。 */
function localWebhookError(code, message) {
  return new AuthApiError({
    status: 422,
    code,
    message,
    requestId: "-",
    traceId: "-",
  });
}

/** 校验来自当前 tenant-qualified 页面快照的不透明 ID；空值不允许进入资源路径。 */
function requireOpaqueId(value, code) {
  const normalized = String(value ?? "").trim();
  if (!normalized || normalized.length > 160) {
    // 空值或超长 ID 不能进入资源路径，统一映射公开错误且不回显原始输入。
    throw localWebhookError(code, "Webhook 死信资源不存在");
  }
  return normalized;
}
