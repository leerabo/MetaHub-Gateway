import { fetchHealthWithNetworkRetry } from "./healthRequest.js";

/**
 * 封装 前端 API 客户端 的 `getSystemHealth` 调用，统一处理请求参数、认证头和错误对象。
 * 边界：客户端只负责展示和发起请求，真实权限仍由后端 API 校验。
 */
export async function getSystemHealth({ gatewayUrl, controlApiUrl }) {
  const [gateway, control] = await Promise.all([
    readServiceHealth("Gateway Data Plane", gatewayUrl, "gateway"),
    readServiceHealth("Control Plane API", controlApiUrl, "control"),
  ]);
  return { services: [gateway, control] };
}

/**
 * 封装 前端 API 客户端 的 `readServiceHealth` 调用，统一处理请求参数、认证头和错误对象。
 * 边界：客户端只负责展示和发起请求，真实权限仍由后端 API 校验。
 */
async function readServiceHealth(name, baseUrl, kind) {
  try {
    const [live, ready, version] = await Promise.all([
      readJson(`${baseUrl}/health/live`, `${kind}_live_${Date.now()}`),
      readReady(`${baseUrl}/health/ready`, `${kind}_ready_${Date.now()}`),
      readJson(`${baseUrl}/version`, `${kind}_version_${Date.now()}`),
    ]);
    return { name, state: "ok", live, ready, version };
  } catch (error) {
    return {
      name,
      state: "error",
      message: "健康检查接口不可用",
    };
  }
}

/**
 * 封装 前端 API 客户端 的 `readReady` 调用，统一处理请求参数、认证头和错误对象。
 * 边界：客户端只负责展示和发起请求，真实权限仍由后端 API 校验。
 */
async function readReady(url, requestId) {
  const response = await fetchHealthWithNetworkRetry(url, requestId);
  const payload = await response.json();
  // ready=false 是正常可解释状态，不应该被当成页面请求失败。
  return payload;
}

/**
 * 封装 前端 API 客户端 的 `readJson` 调用，统一处理请求参数、认证头和错误对象。
 * 边界：客户端只负责展示和发起请求，真实权限仍由后端 API 校验。
 */
async function readJson(url, requestId) {
  const response = await fetchHealthWithNetworkRetry(url, requestId);
  if (!response.ok) {
    // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    throw new Error(`health endpoint failed: ${response.status}`);
  }
  return response.json();
}
