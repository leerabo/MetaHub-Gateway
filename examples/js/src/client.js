/**
 * 负责在可信 Node.js 运行时构造官方 OpenAI 客户端，不提供任何浏览器直连开关。
 */

import OpenAI from "openai";

import { ClientError } from "./errors.js";

/**
 * 检测常见浏览器全局对象，在读取 Virtual Key 或创建官方客户端前立即失败。
 */
export function assertNodeRuntime() {
  // 浏览器环境不能持有长期 Virtual Key；即使构建工具提供了 process 垫片也必须拒绝。
  if (typeof window !== "undefined" || typeof document !== "undefined") {
    throw new ClientError("client.browser_runtime_forbidden");
  }
}

/**
 * 使用公开构造参数创建指向 MetaHub Gateway 的 OpenAI-compatible 客户端。
 */
export function createOpenAIClient(config, { OpenAIClass = OpenAI } = {}) {
  assertNodeRuntime();
  // Virtual Key 只传入官方 apiKey 参数，配置对象不得被日志或异常序列化。
  return new OpenAIClass({
    apiKey: config.virtualKey,
    baseURL: config.baseUrl,
    timeout: config.timeoutMilliseconds,
    maxRetries: config.maxRetries,
    logLevel: "off",
  });
}
