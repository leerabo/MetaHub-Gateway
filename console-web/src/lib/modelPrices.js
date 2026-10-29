import { AuthApiError } from "./auth.js";

/**
 * 查询当前租户模型价格时间线，tenant_id 始终由后端登录态恢复。
 */
export async function listModelPrices({
  controlApiUrl,
  token,
  modelId,
  effectiveStatus = "all",
  limit = 100,
  offset = 0,
}) {
  const url = new URL(`${controlApiUrl}/model-prices`);
  // 模型筛选只提交稳定模型 ID，tenant_id 绝不能由浏览器拼入查询参数。
  if (modelId) url.searchParams.set("model_id", modelId);
  if (effectiveStatus) url.searchParams.set("effective_status", effectiveStatus);
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  // 认证 Token 仅进入 Authorization 请求头，不得进入 URL、日志或页面文本。
  return modelPriceRequest(url, { headers: { Authorization: `Bearer ${token}` } });
}

/**
 * 创建不可变价格计划，金额按字符串提交以避免浏览器浮点转换损失。
 */
export async function createModelPrice({
  controlApiUrl,
  token,
  modelId,
  inputPricePerMillion,
  outputPricePerMillion,
  cachePricePerMillion,
  inputPricePer1k,
  outputPricePer1k,
  cachePricePer1k,
  currency,
  effectiveFrom,
  priceSchemaVersion,
}) {
  return modelPriceRequest(`${controlApiUrl}/model-prices`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model_id: modelId,
      input_price_per_1k: inputPricePerMillion === undefined
        ? String(inputPricePer1k ?? "")
        : String(inputPricePerMillion ?? ""),
      output_price_per_1k: outputPricePerMillion === undefined
        ? String(outputPricePer1k ?? "")
        : String(outputPricePerMillion ?? ""),
      cache_price_per_1k: cachePricePerMillion === undefined
        ? String(
          cachePricePer1k
          ?? inputPricePer1k
          ?? (inputPricePerMillion === undefined ? "" : String(inputPricePerMillion ?? "")),
        )
        : String(cachePricePerMillion ?? ""),
      currency,
      effective_from: effectiveFrom,
      price_schema_version: optionalText(priceSchemaVersion),
    }),
  });
}

/**
 * 一次性提交多个价格时间段；后端保证整批原子提交，避免补录到一半留下缺口。
 */
export async function createModelPricesBatch({ controlApiUrl, token, plans }) {
  if (!Array.isArray(plans) || plans.length < 1 || plans.length > 100) {
    throw new TypeError("plans must contain 1 to 100 price periods");
  }
  return modelPriceRequest(`${controlApiUrl}/model-prices/batch`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      plans: plans.map((plan) => ({
        model_id: plan.modelId,
        input_price_per_1k: String(plan.inputPricePerMillion ?? plan.inputPricePer1k ?? ""),
        output_price_per_1k: String(plan.outputPricePerMillion ?? plan.outputPricePer1k ?? ""),
        cache_price_per_1k: String(
          plan.cachePricePerMillion ?? plan.cachePricePer1k
          ?? plan.inputPricePerMillion ?? plan.inputPricePer1k ?? "",
        ),
        currency: plan.currency,
        effective_from: plan.effectiveFrom,
        price_schema_version: optionalText(plan.priceSchemaVersion),
      })),
    }),
  });
}

/**
 * 发送模型价格请求并保留统一错误体中的 request_id、trace_id 和诊断建议。
 */
async function modelPriceRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_model_price_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 后端权限或业务错误必须原样进入统一前端错误，不能展示网络堆栈或 SQL 文本。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `模型价格接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON；代理返回非 JSON 时使用空对象进入统一错误分支。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/**
 * 把空版本转换为 null，让后端生成稳定人工版本而不是保存空字符串。
 */
function optionalText(value) {
  const text = String(value ?? "").trim();
  return text || null;
}
