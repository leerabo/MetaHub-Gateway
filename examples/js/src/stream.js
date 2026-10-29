/**
 * 提供可注入的 SSE Chat 流式调用，并用 AbortController 覆盖完整流生命周期。
 */

import { pathToFileURL } from "node:url";

import { createOpenAIClient, assertNodeRuntime } from "./client.js";
import { loadConfig } from "./config.js";
import {
  ClientError,
  StreamError,
  formatError,
  safeToken,
} from "./errors.js";
import { parsePrompt } from "./chat.js";

/**
 * 逐段消费官方 SDK 异步流，只写非空文本，并保留响应头握手 request_id。
 */
export async function runStream({
  client,
  config,
  prompt,
  stdout = process.stdout,
  abortControllerFactory = () => new AbortController(),
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
}) {
  const controller = abortControllerFactory();
  let timedOut = false;
  let wroteText = false;
  let sawTerminalChunk = false;
  let handshakeRequestId;
  const timer = setTimeoutFn(() => {
    timedOut = true;
    controller.abort();
  }, config.timeoutMilliseconds);

  try {
    const request = client.chat.completions.create(
      {
        model: config.chatModel,
        messages: [{ role: "user", content: prompt }],
        stream: true,
      },
      { signal: controller.signal },
    );
    const result = await request.withResponse();
    handshakeRequestId = result.request_id;

    for await (const chunk of result.data) {
      if (
        chunk?.choices?.some(
          (choice) =>
            typeof choice?.finish_reason === "string" && choice.finish_reason.length > 0,
        )
      ) {
        sawTerminalChunk = true;
      }
      const content = chunk?.choices?.[0]?.delta?.content;
      // usage、空 choices、工具调用和拒绝块没有文本，不应终止或污染 stdout。
      if (typeof content !== "string" || content.length === 0) {
        continue;
      }
      stdout.write(content);
      wroteText = true;
    }

    // SDK 在底层 abort 或异常 EOF 时可能结束迭代而不抛错，必须结合本地超时标志判定。
    if (timedOut) {
      throw new ClientError("client.stream_timeout", {
        retryable: true,
        requestId: handshakeRequestId,
      });
    }
    // 整个响应没有文本时返回固定空流错误，不能打印原始 chunk 辅助诊断。
    if (!wroteText) {
      throw new ClientError("client.empty_stream", {
        requestId: handshakeRequestId,
      });
    }
    // 已输出正文但未收到 finish_reason 表示 SSE 被截断，不能把不完整答案报告为成功。
    if (!sawTerminalChunk) {
      throw new ClientError("client.connection_failed", {
        retryable: true,
        requestId: handshakeRequestId,
      });
    }
    stdout.write(`\nrequest_id=${safeToken(handshakeRequestId)}\n`);
    return { requestId: handshakeRequestId };
  } catch (error) {
    // 部分正文已经输出时先补换行，确保固定错误行不会粘连在模型文本之后。
    if (wroteText) {
      stdout.write("\n");
    }
    // 定时器触发优先映射为总流式超时，不透传 SDK abort 的异常文本。
    if (timedOut) {
      throw new ClientError("client.stream_timeout", {
        retryable: true,
        requestId: handshakeRequestId,
      });
    }
    // 已完成安全分类的本地错误直接上抛，避免再次包装丢失固定代码。
    if (error instanceof ClientError) {
      throw error;
    }
    // SDK 迭代异常仅附加握手标识，终端层不会输出消息、堆栈或 cause。
    throw new StreamError(error, handshakeRequestId);
  } finally {
    // 无论正常结束、断流还是 abort 都清理定时器，避免 CLI 被悬挂计时器阻塞。
    clearTimeoutFn(timer);
  }
}

/**
 * 组合运行时保护、配置、可注入客户端与流式错误输出，返回稳定进程状态码。
 */
export async function main({
  argv = process.argv.slice(2),
  environ = process.env,
  clientFactory = createOpenAIClient,
  stdout = process.stdout,
  stderr = process.stderr,
  run = runStream,
} = {}) {
  try {
    // 浏览器检查必须先于环境读取，防止长期 Virtual Key 进入错误的运行时。
    assertNodeRuntime();
    const config = loadConfig(environ);
    const prompt = parsePrompt(argv);
    const client = clientFactory(config);
    await run({ client, config, prompt, stdout });
    return 0;
  } catch (error) {
    stderr.write(`${formatError(error)}\n`);
    return 1;
  }
}

/**
 * 判断模块是否由 Node 直接执行，作为库导入时不得自动建立 SSE 连接。
 */
function isDirectExecution() {
  return Boolean(process.argv[1]) && import.meta.url === pathToFileURL(process.argv[1]).href;
}

if (isDirectExecution()) {
  process.exitCode = await main();
}
