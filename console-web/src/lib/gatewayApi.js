// 调试台预览按 UTF-8 字节数限制为 256 KiB，避免异常长流持续占用页面内存。
export const STREAM_PREVIEW_MAX_BYTES = 256 * 1024;

/**
 * 网关调试台的统一异常。
 *
 * 该异常只保留后端允许展示的安全字段；流式错误额外携带已经收到的受限预览和计数，
 * 让界面在失败后仍能展示错误发生前的上下文，但不会保存完整响应或认证信息。
 */
export class GatewayApiError extends Error {
  /**
   * 把握手错误或标准 SSE error 归一化为同一前端错误对象。
   * 关键边界：message 必须来自统一错误体，调用方不得把 Authorization 或原始异常正文传入。
   */
  constructor({
    status,
    code,
    message,
    requestId,
    traceId,
    suggestion,
    diagnosis,
    preview = "",
    previewTruncated = false,
    chunkCount = 0,
    byteCount = 0,
  }) {
    super(message || "网关请求失败");
    this.name = "GatewayApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.traceId = traceId;
    this.suggestion = suggestion;
    this.diagnosis = diagnosis ?? null;
    this.preview = preview;
    this.previewTruncated = previewTruncated;
    this.chunkCount = chunkCount;
    this.byteCount = byteCount;
  }
}

/**
 * 从控制台向 Gateway 发起最小 Chat Completions 调试请求。
 *
 * Virtual Key 只在当前调用栈和 Authorization Header 中短暂使用，不写入任何浏览器存储或返回对象。
 * stream=true 时必须通过 ReadableStream 增量读取；onStreamUpdate 每收到一个非空网络块调用一次。
 */
export async function sendChatCompletion({
  gatewayUrl,
  virtualKey,
  model,
  message,
  stream,
  onStreamUpdate,
  signal,
}) {
  const response = await fetch(`${gatewayUrl.replace(/\/$/, "")}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Request-ID": `console_gateway_${Date.now()}`,
      Authorization: `Bearer ${virtualKey}`,
    },
    body: JSON.stringify({
      model,
      messages: [{ role: "user", content: message }],
      stream: Boolean(stream),
    }),
    // AbortSignal 同时覆盖握手和响应体读取；主动停止必须传播原生 AbortError，不能伪装成网关故障。
    signal,
  });

  // 非 2xx 表示尚未进入成功 SSE 响应，继续读取统一 JSON 错误体并保留链路标识。
  if (!response.ok) {
    const payload = await readJson(response);
    const errorPayload = payload.error && typeof payload.error === "object" ? payload.error : payload;
    // 握手失败只使用统一错误体和响应头构造安全异常，绝不附带请求中的 Virtual Key。
    throw new GatewayApiError({
      status: response.status,
      code: errorPayload.code,
      message: errorPayload.message || `网关接口返回 ${response.status}`,
      requestId: errorPayload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: errorPayload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: errorPayload.suggestion || "",
      diagnosis: errorPayload.diagnosis ?? null,
    });
  }

  const requestId = response.headers.get("X-Request-ID") || "-";
  const traceId = response.headers.get("X-Trace-ID") || "-";
  // 非流式路径保持既有 JSON 契约，不经过 SSE 解析器，也不会触发增量回调。
  if (!stream) {
    const payload = await readJson(response);
    return {
      mode: "json",
      status: "completed",
      requestId,
      traceId,
      preview: JSON.stringify(payload, null, 2),
      payload,
    };
  }

  return readEventStream({ response, requestId, traceId, onStreamUpdate });
}

/**
 * 使用 response.body.getReader() 增量消费 SSE 响应，并返回最终有界快照。
 *
 * TextDecoder 的 stream=true 保证一个 UTF-8 字符被拆到多个 Uint8Array 时不会产生乱码；原始字节计数
 * 直接使用 value.byteLength，预览截断只影响显示，不影响后续网络读取或 SSE error 识别。
 */
async function readEventStream({ response, requestId, traceId, onStreamUpdate }) {
  // 成功响应却没有 ReadableStream 时无法满足增量读取契约，应作为安全、可定位的客户端错误结束。
  if (!response.body || typeof response.body.getReader !== "function") {
    throw new GatewayApiError({
      status: response.status,
      code: "gateway.stream_body_unavailable",
      message: "浏览器未提供可读取的流式响应体。",
      requestId,
      traceId,
      suggestion: "检查浏览器 ReadableStream 支持和网关代理配置。",
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const previewBuffer = createPreviewBuffer(STREAM_PREVIEW_MAX_BYTES);
  const sseParser = createSseErrorParser();
  let chunkCount = 0;
  let byteCount = 0;
  let completed = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      // done=true 表示服务端正常关闭响应体；跳出后还需 flush TextDecoder 中可能残留的字节。
      if (done) {
        completed = true;
        break;
      }
      // 空块不代表可见进度，也不计入调试台块数；继续等待下一个真实传输块。
      if (!value || value.byteLength === 0) {
        continue;
      }

      chunkCount += 1;
      byteCount += value.byteLength;
      const decodedText = decoder.decode(value, { stream: true });
      previewBuffer.append(decodedText);
      const streamError = sseParser.push(decodedText);
      const snapshot = createStreamSnapshot({
        status: streamError ? "failed" : "streaming",
        requestId,
        traceId,
        previewBuffer,
        chunkCount,
        byteCount,
        streamError,
      });
      // 回调可以同步更新 textContent，也可以返回 Promise；等待它可维持浏览器渲染与网络读取之间的自然背压。
      if (typeof onStreamUpdate === "function") {
        await onStreamUpdate(snapshot);
      }
      // 只有结构合法的 event:error 才终止为失败；未知或畸形 SSE 继续作为原始预览读取。
      if (streamError) {
        throw createStreamError({ response, streamError, snapshot });
      }
    }

    const finalText = decoder.decode();
    previewBuffer.append(finalText);
    const finalStreamError = sseParser.push(finalText, { final: true });
    const finalSnapshot = createStreamSnapshot({
      status: finalStreamError ? "failed" : "completed",
      requestId,
      traceId,
      previewBuffer,
      chunkCount,
      byteCount,
      streamError: finalStreamError,
    });
    // decoder flush 可能补出最后一个跨块字符；即使没有新增网络块，也要让最终返回值包含它。
    if (finalStreamError) {
      if (typeof onStreamUpdate === "function") {
        await onStreamUpdate(finalSnapshot);
      }
      // 流关闭时才补全的标准 error 仍必须作为失败抛出，不能被 completed 结果覆盖。
      throw createStreamError({ response, streamError: finalStreamError, snapshot: finalSnapshot });
    }
    return finalSnapshot;
  } finally {
    // 若解析到 SSE error 或回调异常而提前退出，主动 cancel 防止仍可读的响应体继续占用连接。
    if (!completed) {
      try {
        await reader.cancel?.();
      } catch {
        // cancel 失败不覆盖原始 SSE error 或 AbortError，避免用户看到错误的终止原因。
      }
    }
    // cancel 完成后再释放 JS reader 所有权，避免已解锁 reader 无法取消仍在读取的响应体。
    reader.releaseLock?.();
  }
}

/**
 * 创建供回调和最终返回复用的不可变流快照。
 * 关键边界：快照不包含 Virtual Key、Authorization、Prompt、请求对象或完整未截断响应。
 */
function createStreamSnapshot({
  status,
  requestId,
  traceId,
  previewBuffer,
  chunkCount,
  byteCount,
  streamError,
}) {
  return {
    mode: "stream",
    status,
    requestId: streamError?.request_id || requestId,
    traceId: streamError?.trace_id || traceId,
    preview: previewBuffer.value(),
    previewTruncated: previewBuffer.truncated(),
    chunkCount,
    byteCount,
  };
}

/**
 * 把标准 SSE event:error 转成 GatewayApiError，并附带错误发生前的有界预览。
 */
function createStreamError({ response, streamError, snapshot }) {
  return new GatewayApiError({
    status: response.status ?? 200,
    code: streamError.code,
    message: streamError.message,
    requestId: snapshot.requestId,
    traceId: snapshot.traceId,
    suggestion: streamError.suggestion || "",
    diagnosis: streamError.diagnosis ?? null,
    preview: snapshot.preview,
    previewTruncated: snapshot.previewTruncated,
    chunkCount: snapshot.chunkCount,
    byteCount: snapshot.byteCount,
  });
}

/**
 * 创建按 UTF-8 字节数保留最新内容的预览缓冲区。
 * 超限时只删除最旧的完整 Unicode 字符，绝不截断代理对或改变正在进行的网络读取。
 */
function createPreviewBuffer(maxBytes) {
  const encoder = new TextEncoder();
  let preview = "";
  let wasTruncated = false;

  return {
    /** 追加 TextDecoder 已经安全解码的文本，并在超限后保留最新后缀。 */
    append(text) {
      if (!text) {
        return;
      }
      preview += text;
      const encodedLength = encoder.encode(preview).byteLength;
      // 未超过上限时保留完整文本，避免不必要地遍历 Unicode 码点。
      if (encodedLength <= maxBytes) {
        return;
      }
      preview = retainUtf8Suffix(preview, maxBytes, encoder);
      wasTruncated = true;
    },
    /** 返回当前受限预览；调用方只能展示该值，不能访问已被丢弃的旧内容。 */
    value() {
      return preview;
    },
    /** 返回是否曾发生截断，用于给用户明确提示预览并非完整响应。 */
    truncated() {
      return wasTruncated;
    },
  };
}

/**
 * 从字符串末尾按完整 Unicode 码点累计 UTF-8 字节，返回不超过上限的最新后缀。
 */
function retainUtf8Suffix(text, maxBytes, encoder) {
  const codePoints = Array.from(text);
  let usedBytes = 0;
  let startIndex = codePoints.length;
  // 从后向前累计可以直接保留最新输出；一旦再加入一个字符会超限就立即停止。
  for (let index = codePoints.length - 1; index >= 0; index -= 1) {
    const nextBytes = encoder.encode(codePoints[index]).byteLength;
    if (usedBytes + nextBytes > maxBytes) {
      break;
    }
    usedBytes += nextBytes;
    startIndex = index;
  }
  return codePoints.slice(startIndex).join("");
}

/**
 * 创建最小 SSE 解析器，只识别标准 event:error，不解释普通 data 事件或执行任何返回内容。
 * 行缓冲保留跨网络块的字段和 CRLF，确保 event、data 和 JSON 都可被任意拆分。
 */
function createSseErrorParser() {
  let lineBuffer = "";
  let eventName = "";
  let dataLines = [];

  /** 解析一个完整 SSE 字段行；comment/未知字段只保留在原始预览中，不参与错误判断。 */
  function consumeLine(line) {
    // 空行结束当前 SSE 事件，只有 event:error 且 data 为合法统一错误 JSON 时才返回错误。
    if (line === "") {
      const parsedError = eventName === "error" ? parseSseErrorData(dataLines.join("\n")) : null;
      eventName = "";
      dataLines = [];
      return parsedError;
    }
    // 以冒号开头的是 heartbeat/comment，不得误判为模型输出或 error 事件。
    if (line.startsWith(":")) {
      return null;
    }
    const separatorIndex = line.indexOf(":");
    const field = separatorIndex === -1 ? line : line.slice(0, separatorIndex);
    let value = separatorIndex === -1 ? "" : line.slice(separatorIndex + 1);
    // SSE 规范允许冒号后有一个可选空格，该空格不属于字段值。
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }
    if (field === "event") {
      eventName = value;
    } else if (field === "data") {
      dataLines.push(value);
    }
    return null;
  }

  return {
    /**
     * 增量推入已解码文本；final=true 时只消费最后一行，不把缺少空行的半个事件伪判为完整错误。
     */
    push(text, { final = false } = {}) {
      lineBuffer += text;
      while (lineBuffer.length > 0) {
        const lineBreak = findSseLineBreak(lineBuffer, final);
        // 当前块尚未形成完整行时保留缓冲，等待下一次 TextDecoder 输出。
        if (!lineBreak) {
          break;
        }
        const line = lineBuffer.slice(0, lineBreak.index);
        lineBuffer = lineBuffer.slice(lineBreak.index + lineBreak.length);
        const parsedError = consumeLine(line);
        // 一旦取得完整标准 error 就停止继续解析，调用方会取消 reader 并展示有界预览。
        if (parsedError) {
          return parsedError;
        }
      }
      // 流关闭时可以消费最后一个无换行字段，但按 SSE 规范仍不派发缺少空行的事件。
      if (final && lineBuffer) {
        consumeLine(lineBuffer);
        lineBuffer = "";
      }
      return null;
    },
  };
}

/**
 * 查找下一个 SSE 行结束符，并把 CRLF 视为一个分隔符。
 * 若块恰好以 CR 结尾则等待下一块，避免后续 LF 被误当成额外空行而提前派发事件。
 */
function findSseLineBreak(buffer, final) {
  for (let index = 0; index < buffer.length; index += 1) {
    const character = buffer[index];
    if (character === "\n") {
      return { index, length: 1 };
    }
    if (character === "\r") {
      if (index + 1 < buffer.length) {
        return { index, length: buffer[index + 1] === "\n" ? 2 : 1 };
      }
      return final ? { index, length: 1 } : null;
    }
  }
  return null;
}

/**
 * 解析 event:error 的 data JSON，仅接受含 code/message 的统一安全错误对象。
 * 畸形 JSON、数组或缺少必要字段时返回 null，让调用方继续按原始文本展示而不是执行其中内容。
 */
function parseSseErrorData(data) {
  try {
    const payload = JSON.parse(data);
    const error = payload?.error;
    // 只接受统一 code/message 结构，畸形对象继续按普通预览显示而不是执行或信任。
    if (!error || typeof error !== "object" || typeof error.code !== "string" || typeof error.message !== "string") {
      return null;
    }
    return error;
  } catch {
    return null;
  }
}

/**
 * 安全读取 JSON 响应体。
 * 网关错误或代理错误可能不是 JSON；返回空对象可继续使用 HTTP status 和响应头补足上下文。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
