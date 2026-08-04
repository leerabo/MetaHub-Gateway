import { AuthApiError } from "./auth.js";

// 随机源即使被浏览器扩展或测试桩固定，进程内单调序列仍保证并发管理请求不碰撞。
let clientRequestSequence = 0;

/**
 * 查询当前租户的模型别名。
 *
 * status/environment 只作为过滤条件传给后端；租户边界由登录 Token 决定，前端不会
 * 拼接 tenant_id。
 */
export async function listModelAliases({ controlApiUrl, token, status, environmentId, environment, signal }) {
  const url = new URL(`${controlApiUrl}/model-aliases`);
  if (status) {
    url.searchParams.set("status", status);
  }
  if (environment) {
    url.searchParams.set("environment", environment);
  }
  if (environmentId) {
    url.searchParams.set("environment_id", environmentId);
  }
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return modelAliasesRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
}

/**
 * 创建稳定模型别名。
 *
 * default_model_id 是主路由目标，fallback_model_id 可为空；后端负责校验模型是否属于
 * 当前租户，前端只做字段映射。
 */
export async function createModelAlias({
  controlApiUrl,
  token,
  alias,
  environmentId,
  environment,
  defaultModelId,
  fallbackModelId,
  description,
}) {
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return modelAliasesRequest(`${controlApiUrl}/model-aliases`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      alias,
      environment_id: environmentId || null,
      environment,
      default_model_id: defaultModelId,
      fallback_model_id: fallbackModelId || undefined,
      description,
    }),
  });
}

/**
 * 更新模型别名配置。
 *
 * PATCH 只发送页面当前表单字段；禁用别名也走 status 字段，保持审计和错误码一致。
 */
export async function updateModelAlias({
  controlApiUrl,
  token,
  modelAliasId,
  alias,
  environmentId,
  environment,
  defaultModelId,
  fallbackModelId,
  description,
  status,
  expectedRoutingPolicyVersion,
}) {
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return modelAliasesRequest(`${controlApiUrl}/model-aliases/${encodeURIComponent(modelAliasId)}`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      alias,
      environment_id: environmentId || undefined,
      environment,
      default_model_id: defaultModelId,
      fallback_model_id: fallbackModelId,
      description,
      status,
      expected_routing_policy_version: expectedRoutingPolicyVersion,
    }),
  });
}

/**
 * 修改模型别名的非流式 Fallback 策略。
 *
 * 条件只发送后端白名单枚举；清除备用模型时显式发送 null，省略时则保留现有目标。
 */
export async function updateFallbackPolicy({
  controlApiUrl,
  token,
  modelAliasId,
  enabled,
  fallbackModelId,
  triggerConditions,
  clearFallbackModel = false,
  expectedRoutingPolicyVersion,
}) {
  const body = {
    enabled: Boolean(enabled),
    trigger_conditions: triggerConditions,
    // Fallback 与 Alias 主备候选共享 RoutingPolicy 图，必须用同一版本做锁内 CAS。
    expected_routing_policy_version: expectedRoutingPolicyVersion,
  };
  if (clearFallbackModel) {
    body.fallback_model_id = null;
  } else if (fallbackModelId !== undefined) {
    body.fallback_model_id = fallbackModelId;
  }
  return modelAliasesRequest(
    `${controlApiUrl}/model-aliases/${encodeURIComponent(modelAliasId)}/fallback`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    },
  );
}

/**
 * 生成路由模式保存使用的唯一 request_id。
 * 随机标识不包含 tenant、用户、Alias 或模式，既可安全展示，也可在响应丢失时原样重试。
 */
export function createRoutingPolicyRequestId() {
  return `req_console_routing_policy_${createRequestNonce()}`;
}

/**
 * 生成 CacheAffinity 配置保存使用的唯一 request_id。
 * 标识不编码 tenant、Alias、分类或其他业务值，结果未知时由页面按同一意图安全复用。
 */
export function createCacheAffinityRequestId() {
  return `req_console_cache_affinity_${createRequestNonce()}`;
}

/**
 * 以完整快照更新一个 Alias 的 Prompt Cache 亲和配置。
 * 五个 wire 字段逐项构造；tenant、actor、Prompt 与运行时 Binding 只能由后端可信上下文恢复。
 */
export async function updateCacheAffinity({
  controlApiUrl,
  token,
  modelAliasId,
  status,
  dataClass,
  minimumPrefixChars,
  ttlSeconds,
  expectedVersion,
  requestId = createCacheAffinityRequestId(),
  signal,
}) {
  return modelAliasesRequest(
    `${controlApiUrl}/model-aliases/${encodeURIComponent(modelAliasId)}/cache-affinity`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        status,
        data_class: dataClass,
        minimum_prefix_chars: minimumPrefixChars,
        ttl_seconds: ttlSeconds,
        expected_version: expectedVersion,
      }),
      signal,
    },
    requestId,
  );
}

/**
 * 更新一个 ModelAlias 的初始路由模式。
 * 旧三模式请求体固定只有 routing_mode 和 expected_version；quality_cost 才携带完整五字段配置。
 * tenant_id、actor 和候选图必须由后端 Bearer Session 与租户内 Policy 恢复，前端不能提交。
 */
export async function updateRoutingMode({
  controlApiUrl,
  token,
  modelAliasId,
  routingMode,
  expectedVersion,
  qualityCostConfig,
  requestId = createRoutingPolicyRequestId(),
  signal,
}) {
  const body = {
    routing_mode: routingMode,
    expected_version: expectedVersion,
  };
  if (routingMode === "quality_cost") {
    // 显式逐字段复制，避免调用方对象中的 tenant_id、统计值或未来扩展字段越过精确 wire 合同。
    body.quality_cost_config = safeQualityCostConfig(qualityCostConfig);
  }
  return modelAliasesRequest(
    `${controlApiUrl}/model-aliases/${encodeURIComponent(modelAliasId)}/routing-mode`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal,
    },
    requestId,
  );
}

/**
 * 发送模型别名 API 请求并归一化错误。
 *
 * 所有请求都带 X-Request-ID，非 2xx 响应统一转为 AuthApiError，便于页面共用错误态。
 */
async function modelAliasesRequest(url, options = {}, requestId = createModelAliasRequestId()) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": requestId,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 控制面错误体优先，响应头兜底，保证页面一定能展示 request_id。
    const error = new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `模型别名接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || requestId || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
    // 409 可选携带安全 RoutingPolicy 摘要；仅提取白名单对象，不把任意错误 payload 挂到异常上。
    error.currentRoutingPolicy = safeCurrentRoutingPolicy(payload);
    // CacheAffinity 冲突同样只接纳配置白名单；Prompt、Binding、Credential 和摘要永不挂到异常。
    error.currentCacheAffinity = safeCurrentCacheAffinity(payload);
    throw error;
  }
  return { state: "ok", ...payload };
}

/** 为通用 ModelAlias 请求生成不含租户/用户/业务值的唯一链路 ID。 */
function createModelAliasRequestId() {
  return `console_model_aliases_${createRequestNonce()}`;
}

/**
 * 生成不含业务身份的客户端请求随机段。
 * 优先使用浏览器密码学随机源；极旧环境没有 Web Crypto 时，时间与单调序列只承担唯一性兜底，
 * request_id 本身不是认证凭据，不能据此授予权限或推导租户。
 */
function createRequestNonce() {
  const sequence = (++clientRequestSequence).toString(36);
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") {
    return `${cryptoApi.randomUUID().replaceAll("-", "")}_${sequence}`;
  }
  if (typeof cryptoApi?.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    const random = Array.from(
      bytes,
      (byte) => byte.toString(16).padStart(2, "0"),
    ).join("");
    return `${random}_${sequence}`;
  }
  return `${Date.now().toString(36)}_${sequence}`;
}

/**
 * 从版本冲突响应提取安全的当前策略对象。
 * 后端版本可能把对象放在顶层或 details 中；其他错误字段、SQL 和原始正文都不会进入页面状态。
 */
function safeCurrentRoutingPolicy(payload) {
  const candidate = payload?.routing_policy
    ?? payload?.current_routing_policy
    ?? payload?.details?.routing_policy
    ?? payload?.details?.current_routing_policy;
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
    return null;
  }
  return {
    routing_policy_id: candidate.routing_policy_id,
    model_alias_id: candidate.model_alias_id,
    routing_mode: candidate.routing_mode,
    quality_cost_config: safeQualityCostConfig(candidate.quality_cost_config),
    status: candidate.status,
    version: candidate.version,
    candidates: Array.isArray(candidate.candidates)
      ? candidate.candidates.slice(0, 2).map(safeRoutingPolicyCandidate)
      : [],
    warnings: Array.isArray(candidate.warnings) ? candidate.warnings.slice(0, 20) : [],
    created_at: candidate.created_at,
    updated_at: candidate.updated_at,
  };
}

/**
 * 从版本冲突响应提取可选 CacheAffinity 安全快照。
 * 当前统一错误体可能不携带 details；返回 null 后页面会通过同租户 ModelAlias GET 显式刷新。
 */
function safeCurrentCacheAffinity(payload) {
  const candidate = payload?.cache_affinity
    ?? payload?.current_cache_affinity
    ?? payload?.details?.cache_affinity
    ?? payload?.details?.current_cache_affinity;
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
    return null;
  }
  return {
    cache_affinity_id: candidate.cache_affinity_id,
    tenant_id: candidate.tenant_id,
    model_alias_id: candidate.model_alias_id,
    status: candidate.status,
    data_class: candidate.data_class,
    minimum_prefix_chars: candidate.minimum_prefix_chars,
    ttl_seconds: candidate.ttl_seconds,
    version: candidate.version,
    created_at: candidate.created_at,
    updated_at: candidate.updated_at,
  };
}

/**
 * 把质量成本配置压缩为 PATCH/响应共用的五字段白名单。
 * 本函数只负责字段边界，数值范围和权重合计仍由页面与后端分别校验，不能用前端校验替代 RBAC/CAS。
 */
function safeQualityCostConfig(config) {
  return {
    quality_weight_bps: config?.quality_weight_bps,
    cost_weight_bps: config?.cost_weight_bps,
    latency_weight_bps: config?.latency_weight_bps,
    quality_floor_bps: config?.quality_floor_bps,
    minimum_sample_count: config?.minimum_sample_count,
  };
}

/** 把冲突响应中的候选压缩到 ModelAlias API 公布的安全字段，额外字段不进入页面内存。 */
function safeRoutingPolicyCandidate(candidate) {
  return {
    candidate_role: candidate?.candidate_role,
    configured_priority: candidate?.configured_priority,
    model_id: candidate?.model_id,
    internal_model_id: candidate?.internal_model_id,
    display_name: candidate?.display_name,
    model_status: candidate?.model_status,
    provider_code: candidate?.provider_code,
    provider_name: candidate?.provider_name,
    provider_status: candidate?.provider_status,
  };
}

/**
 * 安全读取 JSON 响应。
 *
 * 后端异常或开发代理异常可能返回非 JSON 内容，空对象能让调用方继续走统一错误分支。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
