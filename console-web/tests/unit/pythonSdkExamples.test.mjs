// 本单测覆盖 Python SDK 示例元数据、受保护下载、文件名安全和控制台页面转义。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderPythonSdkExamples } from "../../src/features/python-sdk-examples/renderPythonSdkExamples.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  downloadPythonSdkExample,
  getPythonSdkExample,
  safeArchiveFilename,
} from "../../src/lib/pythonSdkExamples.js";

// 完整成功状态同时放入特殊字符，验证所有后端动态字段都经过 HTML 转义。
const exampleState = {
  state: "ok",
  id: "python-openai-example",
  tenant_id: "tenant_1",
  language: "python",
  title: "Python <示例>",
  description: "官方客户端 & OpenAI-compatible Gateway",
  package_name: "metahub-gateway-examples",
  version: "0.1.0",
  repository_path: "examples/python",
  download_url: "/developer/sdk-examples/python/download",
  gateway_base_url: "http://gateway.local/v1",
  chat_model_alias: "company-chat",
  embedding_model_alias: "company-embedding",
  environment_variables: [
    { name: "METAHUB_VIRTUAL_KEY", required: true, default_value: null, description: "只保存在本地环境变量" },
  ],
  commands: [
    { label: "Chat", command: 'python -m metahub_gateway_examples.chat --prompt "你好"' },
    { label: "Embeddings", command: 'python -m metahub_gateway_examples.embeddings --input "企业网关"' },
  ],
  files: ["README.md", "src/metahub_gateway_examples/chat.py"],
  capabilities: ["chat", "embeddings", "timeout", "retries", "request_id_error_handling"],
  request_id: "req_metadata",
  trace_id: "trc_metadata",
};

/**
 * 构造测试用响应头对象，保持与浏览器 Headers.get 的大小写不敏感行为一致。
 */
function headers(values) {
  const normalized = new Map(Object.entries(values).map(([key, value]) => [key.toLowerCase(), value]));
  return { get(name) { return normalized.get(String(name).toLowerCase()) ?? null; } };
}

test("python sdk metadata client authenticates without tenant id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return { ok: true, headers: headers({}), async json() { return { ...exampleState }; } };
  };

  const result = await getPythonSdkExample({ controlApiUrl: "http://control.local", token: "jwt-token" });

  assert.equal(calls[0].url, "http://control.local/developer/sdk-examples/python");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(JSON.stringify(calls).includes("tenant_id"), false);
  assert.equal(result.embedding_model_alias, "company-embedding");
});

test("python sdk download returns blob filename and request id", async () => {
  const archive = new Blob(["zip-bytes"], { type: "application/zip" });
  globalThis.fetch = async () => ({
    ok: true,
    headers: headers({
      "Content-Disposition": 'attachment; filename="metahub-python-examples-0.1.0.zip"',
      "X-Request-ID": "req_download",
      "X-Trace-ID": "trc_download",
    }),
    async blob() { return archive; },
  });

  const result = await downloadPythonSdkExample({ controlApiUrl: "http://control.local", token: "jwt-token" });

  assert.equal(result.blob, archive);
  assert.equal(result.filename, "metahub-python-examples-0.1.0.zip");
  assert.equal(result.requestId, "req_download");
});

test("python sdk client preserves unified download errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: headers({ "X-Request-ID": "req_header" }),
    async json() {
      // 权限错误体只返回公开错误码和链路标识，不夹带 Token、tenant_id 或归档内容。
      return { code: "auth.permission_denied", message: "Permission denied.", request_id: "req_denied" };
    },
  });

  await assert.rejects(
    () => downloadPythonSdkExample({ controlApiUrl: "http://control.local", token: "viewer-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_denied");
      return true;
    },
  );
});

test("archive filename parser rejects paths and non-zip values", () => {
  assert.equal(safeArchiveFilename('attachment; filename="../../secret.zip"'), "metahub-python-examples.zip");
  assert.equal(safeArchiveFilename('attachment; filename="sample.exe"'), "metahub-python-examples.zip");
  assert.equal(safeArchiveFilename('attachment; filename="sample-1.0.zip"'), "sample-1.0.zip");
});

test("python sdk page escapes metadata and renders runnable commands", () => {
  const html = renderPythonSdkExamples(exampleState);

  assert.match(html, /Python &lt;示例&gt;/);
  assert.doesNotMatch(html, /Python <示例>/);
  assert.match(html, /METAHUB_VIRTUAL_KEY/);
  assert.match(html, /metahub_gateway_examples\.chat/);
  assert.match(html, /metahub_gateway_examples\.embeddings/);
  assert.match(html, /data-download-python-sdk/);
  assert.doesNotMatch(html, /mhv_/);
});

test("developer shell renders sdk examples only for active item", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: {
      user: { display_name: "Dev", email: "dev@example.com", role: "developer", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-15T12:00:00Z" },
    },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "sdk-examples",
        navigation_items: [{
          id: "sdk-examples",
          label: "SDK 示例",
          group: "开发者",
          target_section: "sdk-examples",
          description: "下载示例",
          empty_state_hint: "下载示例",
          order: 105,
        }],
      },
    },
    pythonSdkExampleState: exampleState,
  });

  assert.match(container.innerHTML, /下载 Python 示例/);
  assert.match(container.innerHTML, /company-embedding/);
  assert.doesNotMatch(container.innerHTML, /5分钟快速接入/);
});
