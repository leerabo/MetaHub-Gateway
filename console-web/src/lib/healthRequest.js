let healthRequestSequence = 0;

export const HEALTH_REQUEST_TIMEOUT_MS = 3_000;


/** 创建不携带 URL 或响应正文的固定超时错误。 */
function healthTimeoutError() {
  const error = new Error("health request timed out");
  error.name = "AbortError";
  return error;
}

/**
 * 执行无业务正文的只读健康探测，并只对网络异常重试一次。
 *
 * HTTP 错误响应由调用方按原合同解释，不在这里重试；每次 attempt 使用独立 request_id，便于后端
 * 结构化日志追踪。URL 固定来自应用配置，函数不会拼接 tenant、Token、Prompt 或 Response。
 */
export async function fetchHealthWithNetworkRetry(
  url,
  requestId,
  {
    fetchOperation = globalThis.fetch,
    timeoutMs = HEALTH_REQUEST_TIMEOUT_MS,
    setTimeoutOperation = globalThis.setTimeout,
    clearTimeoutOperation = globalThis.clearTimeout,
  } = {},
) {
  healthRequestSequence += 1;
  const logicalRequestId = `${requestId}_probe_${healthRequestSequence}`;
  const boundedTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0
    ? timeoutMs
    : HEALTH_REQUEST_TIMEOUT_MS;
  let lastError;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const controller = new AbortController();
    let timeoutHandle;
    try {
      const timeout = new Promise((_, reject) => {
        timeoutHandle = setTimeoutOperation(() => {
          controller.abort();
          reject(healthTimeoutError());
        }, boundedTimeout);
      });
      return await Promise.race([
        fetchOperation(url, {
          headers: { "X-Request-ID": `${logicalRequestId}_attempt_${attempt}` },
          signal: controller.signal,
        }),
        timeout,
      ]);
    } catch (error) {
      lastError = error;
    } finally {
      if (timeoutHandle !== undefined) clearTimeoutOperation(timeoutHandle);
    }
  }
  // 两次独立探测均失败后只抛出最终网络错误，由调用方统一映射健康状态。
  throw lastError;
}
