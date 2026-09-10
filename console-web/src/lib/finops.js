/**
 * 模块职责：封装 PAGE-074/075 使用的 FinOps 正式账本 API。
 * 安全边界：客户端永不接受或发送 tenant_id/actor/currency 等服务端权威字段，
 * 真实租户隔离、管理员权限、金额和冲正约束始终由 Control API 再次校验。
 */

import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

// 调整类型与服务端稳定枚举保持一致，前端拒绝未知值只用于减少误提交，不能替代后端校验。
const ADJUSTMENT_TYPES = new Set([
  "manual_debit",
  "manual_credit",
  "reversal",
  "gift_credit",
  "trial_credit",
]);
// 金额、幂等键和微元上限必须与后端 wire 合同一致，但前端校验只用于即时反馈而非权限边界。
const AMOUNT_PATTERN = /^(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,6})?$/;
const IDEMPOTENCY_PATTERN = /^[\x21-\x7E]{8,128}$/;
const MAX_AMOUNT_MICROS = 1_000_000_000_000_000n;

/**
 * 查询当前认证租户的正式账本快照。
 *
 * @param {object} options 控制面地址、Bearer Token、允许筛选和 opaque cursor。
 * @returns {Promise<object>} 服务端账本 DTO，并附加前端统一的 state=ok。
 *
 * 关键约束：查询白名单在此逐项映射，即使调用对象额外携带 tenantId、actorUserId
 * 或任意 SQL 字段也不会进入 URL；后端仍必须拒绝手工构造的越权参数。
 */
export async function listFinOpsLedger({
  controlApiUrl,
  token,
  authentication,
  signal,
  startDate,
  endDate,
  entryType,
  direction,
  quotaType,
  requestId,
  priceSnapshotId,
  relatedEntryId,
  limit = 20,
  cursor,
  operationRequestId,
  operationTraceId,
}) {
  const url = new URL(`${controlApiUrl}/finops/ledger`);
  setOptionalParam(url, "start_date", startDate);
  setOptionalParam(url, "end_date", endDate);
  setOptionalParam(url, "entry_type", entryType);
  setOptionalParam(url, "direction", direction);
  setOptionalParam(url, "quota_type", quotaType);
  setOptionalParam(url, "request_id", requestId);
  setOptionalParam(url, "price_snapshot_id", priceSnapshotId);
  setOptionalParam(url, "related_entry_id", relatedEntryId);
  url.searchParams.set("limit", String(limit));
  setOptionalParam(url, "cursor", cursor);

  const response = await authenticatedResourceFetch(url.toString(), {
    token, authentication, signal,
    headers: {
      Accept: "application/json",
      "X-Request-ID": operationRequestId || createFinOpsRequestId("ledger"),
      "X-Trace-ID": operationTraceId || createFinOpsTraceId(),
    },
  });
  const payload = await readJson(response);
  await throwFinOpsApiError(response, payload, "FinOps 账本读取失败");
  return { state: "ok", ...payload };
}

/**
 * 创建一条只追加财务调整或冲正。
 *
 * @param {object} options 控制面地址、认证信息、幂等键及允许的业务字段。
 * @returns {Promise<object>} Adjustment、Ledger Entry 和链路标识。
 *
 * 金额始终按十进制字符串传输；currency、direction、quota、actor、tenant 和快照
 * 都由服务端从类型、目标分录和认证上下文推导，禁止浏览器自报。
 */
export async function createFinOpsAdjustment({
  controlApiUrl,
  token,
  authentication,
  signal,
  adjustmentType,
  amount,
  reversalOfEntryId,
  relatedRequestId,
  reason,
  idempotencyKey,
  operationRequestId,
  operationTraceId,
}) {
  const body = normalizeAdjustmentPayload({
    adjustmentType,
    amount,
    reversalOfEntryId,
    relatedRequestId,
    reason,
  });
  const safeIdempotencyKey = requireIdempotencyKey(idempotencyKey);
  const response = await authenticatedResourceFetch(`${controlApiUrl}/finops/adjustments`, {
    token, authentication, signal,
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": safeIdempotencyKey,
      "X-Request-ID": operationRequestId || createFinOpsRequestId("adjustment"),
      "X-Trace-ID": operationTraceId || createFinOpsTraceId(),
    },
    body: JSON.stringify(body),
  });
  const payload = await readJson(response);
  await throwFinOpsApiError(response, payload, "FinOps 调整提交失败");
  return { state: "ok", ...payload };
}

/**
 * 生成一次调整生命周期内可复用的幂等键。
 * 随机值不包含租户、用户、金额或原因，失败重试必须复用同一键而不是生成第二笔账。
 */
export function createFinOpsIdempotencyKey() {
  return `finops_${randomToken(32)}`;
}

/**
 * 生成控制台 FinOps 请求 ID，便于错误响应与控制面日志精确关联。
 * 该标识不承载任何业务数据，也不能作为后端幂等边界。
 */
export function createFinOpsRequestId(action = "request") {
  return `console_finops_${action}_${Date.now()}_${randomToken(12)}`;
}

/**
 * 生成一次调整或列表读取的 Trace ID。
 * 调整重试由调用方复用同一 Trace，列表翻页可以为每次 HTTP 请求生成新 Trace。
 */
export function createFinOpsTraceId() {
  return `trc_console_finops_${randomToken(32)}`;
}

/**
 * 只把非空筛选写入 URL；空白字符串不应改变服务端规范化筛选哈希。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 规范化 POST 白名单并执行浏览器侧精确金额和字段组合检查。
 * 这些检查只提供即时反馈；攻击者可绕过浏览器，因此后端必须执行完全相同或更严格的约束。
 */
function normalizeAdjustmentPayload({ adjustmentType, amount, reversalOfEntryId, relatedRequestId, reason }) {
  const safeType = String(adjustmentType ?? "").trim();
  // 未知类型不得进入请求体；后端仍会以独立枚举再次校验手工构造的请求。
  if (!ADJUSTMENT_TYPES.has(safeType)) {
    throw finOpsClientError("finops.adjustment_invalid", "调整类型无效");
  }
  const safeAmount = requireAmount(amount);
  const safeReason = requireReason(reason);
  const safeReversalId = String(reversalOfEntryId ?? "").trim();
  const safeRelatedRequestId = String(relatedRequestId ?? "").trim();

  // 冲正目标是后端派生来源链的唯一入口，缺失时不能创建无目标的反向财务事实。
  if (safeType === "reversal" && !safeReversalId) {
    throw finOpsClientError("finops.adjustment_invalid", "冲正必须选择目标分录");
  }
  // 冲正必须完全继承目标来源，禁止浏览器同时自报关联请求制造歧义链路。
  if (safeType === "reversal" && safeRelatedRequestId) {
    throw finOpsClientError("finops.adjustment_invalid", "冲正不能自报关联请求");
  }
  // 普通调整不能夹带目标分录，否则会绕过后端针对冲正余额和方向的专用校验。
  if (safeType !== "reversal" && safeReversalId) {
    throw finOpsClientError("finops.adjustment_invalid", "非冲正调整不能携带目标分录");
  }

  return {
    adjustment_type: safeType,
    amount: safeAmount,
    ...(safeReversalId ? { reversal_of_entry_id: safeReversalId } : {}),
    ...(safeRelatedRequestId ? { related_request_id: safeRelatedRequestId } : {}),
    reason: safeReason,
  };
}

/**
 * 校验普通十进制金额并以 BigInt 微元检查边界，避免 Number 精度参与财务判断。
 */
function requireAmount(value) {
  // JSON number、指数、符号和超六位小数会破坏精确微元合同，必须在 fetch 前拒绝。
  if (typeof value !== "string" || !AMOUNT_PATTERN.test(value)) {
    throw finOpsClientError("finops.amount_invalid", "金额必须是最多六位小数的普通十进制字符串");
  }
  const [whole, fraction = ""] = value.split(".");
  const micros = BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, "0") || "0");
  // 调整只能是正数且不得超过数据库整数微元上限，避免零额空事实或跨层溢出。
  if (micros <= 0n || micros > MAX_AMOUNT_MICROS) {
    throw finOpsClientError("finops.amount_invalid", "金额超出允许范围");
  }
  return value;
}

/**
 * 校验调整原因的 UTF-8 长度和控制字符。
 * 原因只进入受权限保护的请求正文，调用方不得把它写入日志、URL、Webhook 或错误消息。
 */
function requireReason(value) {
  const normalized = String(value ?? "").trim();
  const byteLength = new TextEncoder().encode(normalized).byteLength;
  // 原因需要可审计但不能携带控制字符或无界正文，服务端会对同一边界再次失败关闭。
  if (!normalized || byteLength > 500 || /[\u0000-\u001F\u007F-\u009F]/u.test(normalized)) {
    throw finOpsClientError("finops.adjustment_invalid", "调整原因必须为 1 至 500 字节且不含控制字符");
  }
  return normalized;
}

/**
 * 校验可见 ASCII 幂等键；客户端只在内存保存，服务端仅持久化摘要。
 */
function requireIdempotencyKey(value) {
  const normalized = String(value ?? "");
  // 空白、控制字符或超长键可能被代理改写，必须在发送前拒绝并保持服务端摘要合同稳定。
  if (!IDEMPOTENCY_PATTERN.test(normalized)) {
    throw finOpsClientError("finops.adjustment_invalid", "幂等键格式无效");
  }
  return normalized;
}

/**
 * 把非成功响应转换为统一 AuthApiError，只保留公开 code/message/request_id/trace_id。
 * 原始响应正文、SQL、原因、Token 和请求头永远不会进入页面错误状态。
 */
async function throwFinOpsApiError(response, payload, fallbackMessage) {
  if (response.ok) {
    return;
  }
  // 非成功响应只能投影公开错误字段，不能把原始响应对象或请求正文传播到页面状态。
  throw new AuthApiError({
    status: response.status,
    code: payload.code || "finops.ledger_unavailable",
    message: payload.message || fallbackMessage,
    requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
    traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
    suggestion: payload.suggestion || "",
    diagnosis: payload.diagnosis ?? null,
  });
}

/**
 * 构造前端本地校验错误；固定文案不反射原因正文或完整请求载荷。
 */
function finOpsClientError(code, message) {
  return new AuthApiError({
    status: 422,
    code,
    message,
    requestId: "-",
    traceId: "-",
    suggestion: "",
    diagnosis: null,
  });
}

/**
 * 容错读取 JSON；代理返回空正文或 HTML 时仍只使用固定安全错误消息。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/**
 * 生成安全随机的 URL/Header 友好片段；旧浏览器缺少 randomUUID 时使用 getRandomValues，
 * 不允许降级到 Math.random 生成财务幂等键。
 */
function randomToken(length) {
  const uuid = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
  if (uuid?.length >= length) {
    return uuid.slice(0, length);
  }
  const cryptoApi = globalThis.crypto;
  if (!cryptoApi?.getRandomValues) {
    // 缺少密码学随机源时禁止降级为可预测幂等键，避免不同调整发生键碰撞。
    throw finOpsClientError("finops.adjustment_invalid", "当前环境缺少安全随机数能力");
  }
  const bytes = new Uint8Array(Math.ceil(length / 2));
  cryptoApi.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("").slice(0, length);
}
