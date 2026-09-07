import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * 查询当前租户的模型目录。
 *
 * Provider、状态和能力都是后端过滤条件；前端不携带 tenant_id，避免跨租户查询入口。
 */
export async function listModels({ controlApiUrl, authentication, token, signal, status, providerId, capability }) {
  const url = new URL(`${controlApiUrl}/models`);
  if (status) {
    url.searchParams.set("status", status);
  }
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  if (providerId) {
    url.searchParams.set("provider_id", providerId);
  }
  if (capability) {
    url.searchParams.set("capability", capability);
  }
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return modelsRequest(url.toString(), {
    authentication, token, signal,
  });
}

/**
 * 创建模型目录条目。
 *
 * 价格、上下文长度和能力标签都会参与后续路由与成本估算，因此这里保持字段名和
 * 后端 DTO 的显式映射，避免表单字段改名时悄悄丢数据。
 */
export async function createModel({
  controlApiUrl,
  token,
  authentication,
  signal,
  internalModelId,
  providerId,
  providerModelId,
  displayName,
  capabilities,
  contextWindow,
  inputPricePer1k,
  outputPricePer1k,
  currency,
  priceSchemaVersion,
  description,
}) {
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return modelsRequest(`${controlApiUrl}/models`, {
    method: "POST",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      internal_model_id: internalModelId,
      provider_id: providerId,
      provider_model_id: providerModelId,
      display_name: displayName,
      capabilities,
      context_window: contextWindow,
      input_price_per_1k: inputPricePer1k,
      output_price_per_1k: outputPricePer1k,
      currency,
      price_schema_version: priceSchemaVersion,
      description,
    }),
  });
}

/**
 * 更新模型目录条目。
 *
 * PATCH 允许修改 Provider 映射、价格版本、能力和状态；后端继续负责租户权限校验。
 */
export async function updateModel({
  controlApiUrl,
  token,
  authentication,
  signal,
  modelId,
  providerId,
  providerModelId,
  displayName,
  capabilities,
  contextWindow,
  inputPricePer1k,
  outputPricePer1k,
  currency,
  priceSchemaVersion,
  description,
  status,
}) {
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return modelsRequest(`${controlApiUrl}/models/${encodeURIComponent(modelId)}`, {
    method: "PATCH",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      provider_id: providerId,
      provider_model_id: providerModelId,
      display_name: displayName,
      capabilities,
      context_window: contextWindow,
      input_price_per_1k: inputPricePer1k,
      output_price_per_1k: outputPricePer1k,
      currency,
      price_schema_version: priceSchemaVersion,
      description,
      status,
    }),
  });
}

/**
 * 发送模型目录 API 请求并统一错误结构。
 *
 * 页面只处理 state/payload 或 AuthApiError，不直接解析多种后端错误形态。
 */
async function modelsRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_models_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 优先使用控制面统一错误体，确保用户能拿到可追踪 request_id。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `模型目录接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 安全读取 JSON 响应体。
 *
 * 控制面正常响应为 JSON；异常 HTML 或空响应会转为空对象交给统一错误处理。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return {};
  }
}
