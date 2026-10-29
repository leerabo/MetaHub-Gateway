import { fetchHealthWithNetworkRetry } from "./healthRequest.js";

/**
 * 封装 前端 API 客户端 的 `getServiceStatuses` 调用，统一处理请求参数、认证头和错误对象。
 * 边界：客户端只负责展示和发起请求，真实权限仍由后端 API 校验。
 */
export async function getServiceStatuses({ gatewayUrl, controlApiUrl, consoleUrl }) {
  const [gateway, control] = await Promise.all([
    readHealth(`${gatewayUrl}/health`, "Gateway Data Plane", "gateway"),
    readHealth(`${controlApiUrl}/health`, "Control Plane API", "control"),
  ]);

  // 控制台本身由静态开发服务器提供，能运行到这里就说明页面空壳可用。
  return [
    gateway,
    control,
    {
      name: "Console Web",
      kind: "console",
      state: "ok",
      requestId: "local",
      endpoint: consoleUrl,
    },
  ];
}

/**
 * 封装 前端 API 客户端 的 `readHealth` 调用，统一处理请求参数、认证头和错误对象。
 * 边界：客户端只负责展示和发起请求，真实权限仍由后端 API 校验。
 */
async function readHealth(endpoint, name, kind) {
  try {
    const response = await fetchHealthWithNetworkRetry(endpoint, `console_${kind}_${Date.now()}`);
    if (!response.ok) {
      // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
      return { name, kind, state: "error", requestId: response.headers.get("X-Request-ID") ?? "-" };
    }
    const payload = await response.json();
    return {
      name,
      kind,
      state: payload.status === "ok" ? "ok" : "error",
      requestId: payload.request_id ?? response.headers.get("X-Request-ID") ?? "-",
      endpoint,
    };
  } catch (error) {
    // PRD-001 本地页需要温和失败：后端没启动时展示不可用，而不是让整个控制台白屏。
    return { name, kind, state: "error", requestId: "-", endpoint };
  }
}
