/**
 * 负责把配置、SDK、HTTP 和流式异常收敛为不会泄露请求内容的固定单行错误。
 */

import {
  APIConnectionError,
  APIConnectionTimeoutError,
  APIError,
} from "openai";

// 远端错误码与请求标识只允许短 ASCII 标识符，防止换行和终端控制字符注入。
const SAFE_TOKEN_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;

/**
 * 表示由示例主动产生的固定本地错误，不保存底层异常消息、正文或配置对象。
 */
export class ClientError extends Error {
  /**
   * 仅保存公开错误码、重试属性和可选握手标识，避免敏感上下文进入错误对象。
   */
  constructor(code, { retryable = false, requestId = undefined } = {}) {
    super(code);
    this.name = "ClientError";
    this.code = code;
    this.retryable = retryable;
    this.requestId = requestId;
  }
}

/**
 * 包装流迭代异常并保留握手 request_id，原异常只供安全分类逻辑读取公开字段。
 */
export class StreamError extends Error {
  /**
   * 保存原始异常引用和握手标识，但终端格式化绝不会输出 cause、message 或 stack。
   */
  constructor(originalError, handshakeRequestId) {
    super("client.stream_failed");
    this.name = "StreamError";
    this.originalError = originalError;
    this.handshakeRequestId = handshakeRequestId;
  }
}

/**
 * 只接受完整匹配白名单的单行短标识，非法输入统一退化为安全占位符。
 */
export function safeToken(value, fallback = "-") {
  if (typeof value === "string" && SAFE_TOKEN_PATTERN.test(value)) {
    return value;
  }
  return fallback;
}

/**
 * 从 Headers 或兼容对象读取 x-request-id，读取失败时不得暴露对象内部信息。
 */
function requestIdFromHeaders(headers) {
  try {
    if (headers && typeof headers.get === "function") {
      return headers.get("x-request-id") ?? headers.get("X-Request-ID");
    }
    if (headers && typeof headers === "object") {
      return headers["x-request-id"] ?? headers["X-Request-ID"];
    }
  } catch {
    return undefined;
  }
  return undefined;
}

/**
 * 按 SDK 公共 requestID、兼容 request_id、响应头和流握手标识的顺序提取请求 ID。
 */
export function extractRequestId(error, handshakeRequestId = undefined) {
  const candidates = [
    error?.requestID,
    error?.request_id,
    requestIdFromHeaders(error?.headers),
    handshakeRequestId,
  ];
  for (const candidate of candidates) {
    const safe = safeToken(candidate, "");
    if (safe) {
      return safe;
    }
  }
  return "-";
}

/**
 * 判断公开 HTTP 状态是否属于有限重试通常可恢复的范围。
 */
function isRetryableStatus(status) {
  return status === 408 || status === 409 || status === 429 || status >= 500;
}

/**
 * 将任意异常转换成允许写入 stderr 的最小字段集合，不读取 message 或响应正文。
 */
export function toSafeFailure(error, { handshakeRequestId = undefined } = {}) {
  let source = error;
  let handshake = handshakeRequestId;
  // 流式包装只暴露公开分类字段，并把握手标识作为最低优先级回退。
  if (error instanceof StreamError) {
    source = error.originalError;
    handshake = error.handshakeRequestId ?? handshake;
  }

  // 本地错误代码由源码固定产生，可直接通过同一 ASCII 白名单后输出。
  if (source instanceof ClientError) {
    return {
      code: safeToken(source.code, "client.unexpected_error"),
      status: undefined,
      requestId: extractRequestId(source, source.requestId ?? handshake),
      retryable: source.retryable,
    };
  }
  // SDK 建连或响应头超时不具备 HTTP 状态，但属于可有限重试类别。
  if (source instanceof APIConnectionTimeoutError) {
    return {
      code: "client.timeout",
      status: undefined,
      requestId: extractRequestId(source, handshake),
      retryable: true,
    };
  }
  // 网络连接失败不能输出端点和底层消息，只保留固定可重试错误代码。
  if (source instanceof APIConnectionError) {
    return {
      code: "client.connection_failed",
      status: undefined,
      requestId: extractRequestId(source, handshake),
      retryable: true,
    };
  }
  // HTTP API 异常只读取 SDK 公共状态、代码和标识，不解析消息或正文。
  if (source instanceof APIError) {
    const status = Number.isInteger(source.status) ? source.status : undefined;
    return {
      code: safeToken(source.code, "client.api_error"),
      status,
      requestId: extractRequestId(source, handshake),
      retryable: status !== undefined && isRetryableStatus(status),
    };
  }
  return {
    code: "client.unexpected_error",
    status: undefined,
    requestId: extractRequestId(source, handshake),
    retryable: false,
  };
}

/**
 * 按 PRD 固定字段顺序渲染单行错误，未知状态和请求标识使用连字符占位。
 */
export function formatError(error, options = {}) {
  const failure = toSafeFailure(error, options);
  const status = failure.status ?? "-";
  return `error_code=${failure.code} status=${status} request_id=${failure.requestId} retryable=${failure.retryable}`;
}
