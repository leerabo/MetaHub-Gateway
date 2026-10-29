/**
 * PRD-063 Adapter 规范只读客户端。
 *
 * 浏览器只提交版本化的公开筛选，不提交 tenant_id、Provider Endpoint、凭据或 spec 正文；
 * 租户身份必须由 Control API 根据 Bearer Session 恢复。
 */

import { AuthApiError } from "./auth.js";

// API 只接受这两个公开枚举筛选。额外对象属性必须在构造 URL 前被忽略，避免租户或秘密进入查询串。
const FILTER_KEYS = Object.freeze(["classification", "status"]);
const CLASSIFICATIONS = new Set([
  "model_vendor_cloud",
  "local_compatible",
  "customer_managed",
  "migration_platform",
]);
// 状态、鉴权、Adapter 和 Provider 分类共同构成浏览器可接受的冻结合同，未知枚举必须整批拒绝。
const SPEC_STATUSES = new Set(["active", "optional"]);
const AUTH_TYPES = new Set(["none", "bearer"]);
const ADAPTER_NAMES = new Set(["openai_compatible", "generic_http"]);
const PROVIDER_CLASSIFICATION = Object.freeze({
  zhipu: "model_vendor_cloud",
  dashscope: "model_vendor_cloud",
  wenxin: "model_vendor_cloud",
  volcengine: "model_vendor_cloud",
  hunyuan: "model_vendor_cloud",
  deepseek: "model_vendor_cloud",
  openai_compatible: "local_compatible",
  custom: "customer_managed",
  migration_compatible: "migration_platform",
});
// 价格字段顺序也是版本化合同的一部分，避免同一目录产生多种序列化或遗漏币种版本。
const PRICE_FIELDS = Object.freeze([
  "input_price_per_1k",
  "output_price_per_1k",
  "currency",
  "price_schema_version",
]);
// 单条规范仅复制这些公开文本字段，Endpoint、tenant_id、Header 与凭据没有进入 DTO 的通道。
const SPEC_FIELDS = Object.freeze([
  "spec_id",
  "provider_type",
  "display_name",
  "classification",
  "adapter_name",
  "adapter_version",
  "protocol_family",
  "model_id_strategy",
  "usage_profile",
  "error_profile",
  "stream_profile",
  "probe_profile",
  "status",
  "contract_case_id",
]);

/**
 * 查询当前认证租户已激活的 Adapter 规范目录。
 *
 * @param {{controlApiUrl: string, token: string, filters?: object, signal?: AbortSignal}} input 请求参数。
 * @returns {Promise<object>} 经过字段白名单复制的公开目录。
 * @throws {AuthApiError} 后端统一错误或响应合同损坏时抛出，不保留原始响应正文。
 */
export async function listAdapterSpecs({ controlApiUrl, token, filters = {}, signal }) {
  const url = new URL(`${controlApiUrl}/adapter-specs`);
  for (const key of FILTER_KEYS) {
    const value = filters[key];
    if (value !== undefined && value !== null && value !== "") {
      const normalized = String(value).trim().toLowerCase();
      if (
        (key === "classification" && !CLASSIFICATIONS.has(normalized))
        || (key === "status" && !SPEC_STATUSES.has(normalized))
      ) {
        // 非白名单值必须在构造 URL 和发起网络请求前失败，且错误消息不回显被篡改内容。
        throw invalidFilterError();
      }
      url.searchParams.set(key, normalized);
    }
  }
  const response = await fetch(url.toString(), {
    signal,
    headers: {
      Authorization: `Bearer ${token}`,
      "X-Request-ID": `console_adapter_specs_${Date.now()}`,
    },
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    // 代理 HTML 或损坏 JSON 不能进入页面；错误分支只保留统一链路标识。
    payload = {};
  }
  if (!response.ok) {
    // 后端错误只转译统一字段和链路 ID，代理正文或未知扩展字段不能进入页面状态。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `Adapter 规范接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return sanitizeAdapterSpecPayload(payload, response.headers);
}

/** 构造不回显篡改值的本地筛选错误；非法值不会进入 URL，也不会发出网络请求。 */
function invalidFilterError() {
  return new AuthApiError({
    status: 422,
    code: "adapter.spec_filter_invalid",
    message: "Adapter 规范筛选无效",
    requestId: "-",
    traceId: "-",
    suggestion: "请重置筛选后重试。",
  });
}

/**
 * 把成功响应复制成固定公开 DTO。
 *
 * 额外字段一律丢弃，因此未来后端内部字段或错误配置不会被页面意外展示、缓存或序列化。
 */
export function sanitizeAdapterSpecPayload(payload, headers = new Map()) {
  const requestId = safeText(payload?.request_id, 160, "");
  const traceId = safeText(payload?.trace_id, 160, "");
  const catalog = payload?.catalog;
  const specs = payload?.specs;
  const catalogHash = safeText(catalog?.hash, 64, "");
  if (
    !catalog
    || !Array.isArray(specs)
    || specs.length > 64
    || !/^[a-f0-9]{64}$/.test(catalogHash)
  ) {
    // 顶层目录缺失、数量越界或 hash 损坏都表示执行合同不可验证，禁止宽松展示部分结果。
    throw invalidResponseError(requestId, traceId, headers);
  }
  const version = safeText(catalog.version, 80, "");
  const activatedAt = safeText(catalog.activated_at, 64, "");
  if (!version.trim() || !activatedAt || Number.isNaN(Date.parse(activatedAt))) {
    // 版本或激活时间无效时无法确认当前租户目录身份，必须按损坏响应失败关闭。
    throw invalidResponseError(requestId, traceId, headers);
  }

  const sanitizedSpecs = specs.map((item) => sanitizeSpec(item));
  const providerTypes = sanitizedSpecs.map((item) => item?.provider_type);
  const specIds = sanitizedSpecs.map((item) => item?.spec_id);
  if (
    sanitizedSpecs.some((item) => item === null)
    || new Set(providerTypes).size !== providerTypes.length
    || new Set(specIds).size !== specIds.length
  ) {
    // 任一条规范损坏或唯一键重复都会让目录语义产生歧义，不能只丢弃坏行后继续渲染。
    throw invalidResponseError(requestId, traceId, headers);
  }
  return {
    state: "ok",
    catalog: Object.freeze({
      version,
      hash: catalogHash,
      activated_at: activatedAt,
    }),
    specs: Object.freeze(sanitizedSpecs),
    request_id: requestId || headers.get?.("X-Request-ID") || "-",
    trace_id: traceId || headers.get?.("X-Trace-ID") || "-",
  };
}

/** 校验并复制单条公开 spec；任何枚举或字段形状漂移都会让整批响应失败关闭。 */
function sanitizeSpec(item) {
  if (!item || typeof item !== "object") return null;
  const result = {};
  for (const field of SPEC_FIELDS) {
    result[field] = safeText(item[field], 160, "");
  }
  if (
    // 冻结目录的这些文本字段共同描述可执行合同；缺一项就不能降级成页面上的短横线继续展示。
    SPEC_FIELDS.some((field) => !result[field].trim())
    || PROVIDER_CLASSIFICATION[result.provider_type] !== result.classification
    || !ADAPTER_NAMES.has(result.adapter_name)
    || !CLASSIFICATIONS.has(result.classification)
    || !SPEC_STATUSES.has(result.status)
  ) {
    return null;
  }
  const allowedAuthTypes = safeStringArray(item.allowed_auth_types, 4, 40);
  const priceFields = safeStringArray(item.price_fields, 12, 80);
  const platformExamples = safeStringArray(item.platform_examples ?? [], 8, 80, true);
  if (
    !allowedAuthTypes
    || allowedAuthTypes.some((value) => !AUTH_TYPES.has(value))
    || !priceFields
    || priceFields.join("\u0000") !== PRICE_FIELDS.join("\u0000")
    || !platformExamples
    || (result.provider_type === "migration_compatible"
      ? platformExamples.join("\u0000") !== "OpenRouter\u0000硅基流动"
      : platformExamples.length !== 0)
  ) return null;
  return Object.freeze({
    ...result,
    allowed_auth_types: Object.freeze(allowedAuthTypes),
    price_fields: Object.freeze(priceFields),
    platform_examples: Object.freeze(platformExamples),
  });
}

/** 复制有界字符串数组，阻断控制字符、大对象和非字符串进入 DOM。 */
function safeStringArray(value, maximumItems, maximumLength, allowEmpty = false) {
  if (!Array.isArray(value) || value.length > maximumItems || (!allowEmpty && value.length === 0)) {
    return null;
  }
  const result = value.map((item) => safeText(item, maximumLength, ""));
  // 重复鉴权或平台值会制造同一合同的多种序列化形式，必须在进入 DOM 前拒绝整批响应。
  return result.some((item) => !item) || new Set(result).size !== result.length ? null : result;
}

/** 返回有界无控制字符文本；fallback 只用于缺省公开字段，不拼接原始异常内容。 */
function safeText(value, maximumLength, fallback) {
  if (typeof value !== "string" || !value || value.length > maximumLength) return fallback;
  return /[\u0000-\u001f\u007f]/.test(value) ? fallback : value;
}

/** 构造固定的本地合同错误，拒绝把损坏 JSON 或未知字段内容带到页面。 */
function invalidResponseError(requestId, traceId, headers) {
  return new AuthApiError({
    status: 502,
    code: "adapter.spec_response_invalid",
    message: "Adapter 规范响应格式无效",
    requestId: requestId || headers.get?.("X-Request-ID") || "-",
    traceId: traceId || headers.get?.("X-Trace-ID") || "-",
    suggestion: "请稍后重试或联系平台管理员。",
  });
}
