/**
 * 封装 前端 API 客户端 的 `getConfigSummary` 调用，统一处理请求参数、认证头和错误对象。
 * 边界：客户端只负责展示和发起请求，真实权限仍由后端 API 校验。
 */
export async function getConfigSummary({ controlApiUrl, internalToken }) {
  const headers = { "X-Request-ID": `console_config_${Date.now()}` };
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (internalToken) {
    headers["X-Internal-Token"] = internalToken;
  }

  try {
    const response = await fetch(`${controlApiUrl}/internal/config/summary`, { headers });
    if (!response.ok) {
      return {
        state: "error",
        message: `配置摘要接口返回 ${response.status}`,
        requestId: response.headers.get("X-Request-ID") ?? "-",
      };
    }
    const payload = await response.json();
    return { state: "ok", ...payload };
  } catch (error) {
    // 环境信息页是运维辅助入口，不能因为控制面暂时不可用导致整个控制台白屏。
    return {
      state: "error",
      message: "无法连接控制面配置摘要接口",
      requestId: "-",
    };
  }
}
