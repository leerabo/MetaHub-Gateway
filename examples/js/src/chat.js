/**
 * 提供可注入的非流式 Chat 调用和命令行入口，只输出模型文本与安全 request_id。
 */

import { pathToFileURL } from "node:url";

import { createOpenAIClient, assertNodeRuntime } from "./client.js";
import { loadConfig } from "./config.js";
import { ClientError, formatError, safeToken } from "./errors.js";

/**
 * 解析唯一受支持的 --prompt 参数，未知参数与缺失值均在本地安全失败。
 */
export function parsePrompt(argv) {
  // 命令行只接受单一 prompt 参数，避免未知选项被误当成敏感请求内容。
  if (!Array.isArray(argv) || argv.length !== 2 || argv[0] !== "--prompt") {
    throw new ClientError("client.invalid_arguments");
  }
  // 空白 Prompt 在本地拒绝，固定错误中不会包含用户提供的原始字符串。
  if (typeof argv[1] !== "string" || argv[1].trim() === "") {
    throw new ClientError("client.missing_prompt");
  }
  return argv[1];
}

/**
 * 执行一次非流式 Chat 请求，不打印完整响应、Prompt、用量或其他响应元数据。
 */
export async function runChat({ client, config, prompt, stdout = process.stdout }) {
  const response = await client.chat.completions.create({
    model: config.chatModel,
    messages: [{ role: "user", content: prompt }],
    stream: false,
  });
  const content = response?.choices?.[0]?.message?.content;
  // 空响应不能被当作成功结果，也不能通过打印完整响应来尝试诊断。
  if (typeof content !== "string" || content.length === 0) {
    throw new ClientError("client.empty_response", {
      requestId: response?._request_id,
    });
  }
  stdout.write(`${content}\nrequest_id=${safeToken(response?._request_id)}\n`);
  return response;
}

/**
 * 组合运行时保护、配置、可注入客户端与安全错误输出，返回适合进程退出的状态码。
 */
export async function main({
  argv = process.argv.slice(2),
  environ = process.env,
  clientFactory = createOpenAIClient,
  stdout = process.stdout,
  stderr = process.stderr,
} = {}) {
  try {
    // 先阻断浏览器运行时，再读取进程中的长期 Virtual Key。
    assertNodeRuntime();
    const config = loadConfig(environ);
    const prompt = parsePrompt(argv);
    const client = clientFactory(config);
    await runChat({ client, config, prompt, stdout });
    return 0;
  } catch (error) {
    stderr.write(`${formatError(error)}\n`);
    return 1;
  }
}

/**
 * 判断模块是否由 Node 直接执行，导入测试时不得自动发起 Gateway 请求。
 */
function isDirectExecution() {
  return Boolean(process.argv[1]) && import.meta.url === pathToFileURL(process.argv[1]).href;
}

if (isDirectExecution()) {
  process.exitCode = await main();
}
