// 本单测覆盖 PRD-039 JavaScript SDK 元数据、下载安全、双语言 tabs、独立错误状态和完整键盘映射。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  renderJavascriptSdkExamples,
  renderSdkExamples,
  sdkLanguageForKey,
} from "../../src/features/python-sdk-examples/renderPythonSdkExamples.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  downloadJavascriptSdkExample,
  getJavascriptSdkExample,
  safeArchiveFilename,
} from "../../src/lib/pythonSdkExamples.js";

// JavaScript 成功状态在所有动态字段中放入特殊字符，用于验证 HTML、命令、路径和安全提示统一转义。
const javascriptState = {
  state: "ok",
  id: "javascript-openai-example",
  tenant_id: "tenant_1",
  language: "javascript",
  title: "JavaScript <Node> 示例",
  description: "官方 SDK & OpenAI-compatible Gateway",
  package_name: "@metahub/examples",
  version: "0.1.0",
  runtime: "Node.js >= 22 <LTS>",
  repository_path: "examples/js/<unsafe>",
  download_url: "/developer/sdk-examples/javascript/download",
  gateway_base_url: "http://gateway.local/v1?mode=<node>",
  chat_model_alias: "company-<chat>",
  environment_variables: [
    {
      name: "METAHUB_VIRTUAL_KEY",
      required: true,
      default_value: null,
      description: "只保存在 Node 进程 & 环境变量",
    },
  ],
  commands: [
    { id: "install", label: "安装 <依赖>", command: "npm ci && echo <ready>" },
    { id: "configure", label: "准备环境", command: "Copy-Item .env.example .env" },
    { id: "chat", label: "运行 Chat", command: 'node src/chat.js --prompt "你好"' },
    { id: "stream", label: "运行 Stream", command: 'node src/stream.js --prompt "流式"' },
  ],
  files: ["README.md", "src/<client>.js"],
  capabilities: ["chat", "stream", "timeout", "retries", "request_id"],
  browser_direct_supported: false,
  security_notice: "禁止浏览器保存 <长期 Key> & 直连 Gateway。",
  request_id: "req_js_metadata",
  trace_id: "trc_js_metadata",
};

// Python 最小成功状态用于验证 JavaScript 独立错误不会清空另一语言面板。
const pythonState = {
  state: "ok",
  title: "Python SDK 示例",
  version: "0.1.0",
  gateway_base_url: "http://gateway.local/v1",
  chat_model_alias: "company-chat",
  embedding_model_alias: "company-embedding",
  repository_path: "examples/python",
  environment_variables: [],
  commands: [],
  files: [],
  capabilities: [],
};

/**
 * 构造测试响应头对象，保持与浏览器 Headers.get 的大小写不敏感行为一致。
 */
function headers(values) {
  const normalized = new Map(Object.entries(values).map(([key, value]) => [key.toLowerCase(), value]));
  return { get(name) { return normalized.get(String(name).toLowerCase()) ?? null; } };
}

// 元数据请求必须命中固定 JavaScript 路径、携带 Bearer 与请求标识，且不得发送 tenant_id。
test("javascript sdk metadata client authenticates without tenant id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return { ok: true, headers: headers({}), async json() { return { ...javascriptState }; } };
  };

  const result = await getJavascriptSdkExample({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
  });

  assert.equal(calls[0].url, "http://control.local/developer/sdk-examples/javascript");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.match(calls[0].options.headers["X-Request-ID"], /^console_javascript_sdk_/);
  assert.equal(JSON.stringify(calls).includes("tenant_id"), false);
  assert.equal(result.runtime, "Node.js >= 22 <LTS>");
});

// JavaScript 下载必须返回 Blob、响应 request_id，并使用语言专属安全文件名。
test("javascript sdk download returns safe filename and request id", async () => {
  const archive = new Blob(["zip-bytes"], { type: "application/zip" });
  globalThis.fetch = async () => ({
    ok: true,
    headers: headers({
      "Content-Disposition": 'attachment; filename="metahub-javascript-examples-0.1.0.zip"',
      "X-Request-ID": "req_js_download",
      "X-Trace-ID": "trc_js_download",
    }),
    async blob() { return archive; },
  });

  const result = await downloadJavascriptSdkExample({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
  });

  assert.equal(result.blob, archive);
  assert.equal(result.filename, "metahub-javascript-examples-0.1.0.zip");
  assert.equal(result.requestId, "req_js_download");
  assert.equal(result.traceId, "trc_js_download");
});

// 响应头中的路径、非 ASCII、控制字符和错误扩展名必须回退 JavaScript 专属 ZIP 文件名。
test("javascript archive filename fallback rejects unsafe response headers", () => {
  const fallback = "metahub-javascript-examples.zip";
  assert.equal(safeArchiveFilename('attachment; filename="../../secret.zip"', "javascript"), fallback);
  assert.equal(safeArchiveFilename('attachment; filename="示例.zip"', "javascript"), fallback);
  assert.equal(safeArchiveFilename('attachment; filename="sample.zip\r\nX-Test: yes"', "javascript"), fallback);
  assert.equal(safeArchiveFilename('attachment; filename="sample.tar.gz"', "javascript"), fallback);
  assert.equal(safeArchiveFilename('attachment; filename="sample-1.0.zip"', "javascript"), "sample-1.0.zip");
});

// 401 必须保持统一 AuthApiError，供主应用执行唯一的会话清理分支并停止顺序加载。
test("javascript sdk client preserves unified unauthorized error", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 401,
    headers: headers({ "X-Request-ID": "req_header" }),
    async json() {
      // 模拟统一 401 响应，确保正文 request_id 优先于响应头并触发主应用唯一会话清理分支。
      return { code: "auth.unauthorized", message: "Session expired.", request_id: "req_unauthorized" };
    },
  });

  await assert.rejects(
    () => getJavascriptSdkExample({ controlApiUrl: "http://control.local", token: "expired-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.status, 401);
      assert.equal(error.code, "auth.unauthorized");
      assert.equal(error.requestId, "req_unauthorized");
      return true;
    },
  );
});

// JavaScript 面板必须转义全部元数据并展示后端下发的 Node 服务端安全边界。
test("javascript sdk panel escapes metadata and renders server-only security notice", () => {
  const html = renderJavascriptSdkExamples(javascriptState);

  assert.match(html, /JavaScript &lt;Node&gt; 示例/);
  assert.match(html, /Node\.js &gt;= 22 &lt;LTS&gt;/);
  assert.match(html, /npm ci &amp;&amp; echo &lt;ready&gt;/);
  assert.match(html, /src\/&lt;client&gt;\.js/);
  assert.match(html, /禁止浏览器保存 &lt;长期 Key&gt; &amp; 直连 Gateway/);
  assert.match(html, /仅用于 Node\.js 服务端/);
  assert.doesNotMatch(html, /<input[^>]+Virtual Key/i);
  assert.doesNotMatch(html, /dangerouslyAllowBrowser/);
});

// 双语言 tabs 必须拥有唯一关联 ID、roving tabindex 和单一可见 panel，并保留两个语言内容。
test("sdk language tabs render complete aria relationships and active language", () => {
  const html = renderSdkExamples({
    pythonState,
    javascriptState,
    activeLanguage: "javascript",
  });

  assert.match(html, /role="tablist"/);
  assert.match(html, /id="sdk-language-tab-python"[\s\S]*aria-controls="sdk-language-panel-python"[\s\S]*tabindex="-1"/);
  assert.match(html, /id="sdk-language-tab-javascript"[\s\S]*aria-selected="true"[\s\S]*tabindex="0"/);
  assert.match(html, /id="sdk-language-panel-python"[\s\S]*aria-labelledby="sdk-language-tab-python"[\s\S]*hidden/);
  assert.match(html, /id="sdk-language-panel-javascript"[\s\S]*aria-labelledby="sdk-language-tab-javascript"/);
  assert.equal((html.match(/role="tab"/g) ?? []).length, 2);
  assert.equal((html.match(/role="tabpanel"/g) ?? []).length, 2);
});

// 非 401 的 JavaScript 错误只能出现在 JavaScript panel，Python 成功元数据必须继续存在。
test("sdk language panels keep independent error states", () => {
  const html = renderSdkExamples({
    pythonState,
    javascriptState: { state: "error", message: "JavaScript 制品不可用", requestId: "req_js_error" },
    activeLanguage: "python",
  });

  assert.match(html, /company-embedding/);
  assert.match(html, /JavaScript 制品不可用/);
  assert.match(html, /request_id: req_js_error/);
  assert.doesNotMatch(html, /Python SDK 示例暂不可用/);
});

// 方向键循环、Home/End 和 Enter/Space 均应映射到明确语言，Tab 与普通字符不得被拦截。
test("sdk language keyboard mapping covers arrows home end enter and space", () => {
  assert.equal(sdkLanguageForKey("python", "ArrowRight"), "javascript");
  assert.equal(sdkLanguageForKey("javascript", "ArrowRight"), "python");
  assert.equal(sdkLanguageForKey("python", "ArrowLeft"), "javascript");
  assert.equal(sdkLanguageForKey("javascript", "Home"), "python");
  assert.equal(sdkLanguageForKey("python", "End"), "javascript");
  assert.equal(sdkLanguageForKey("javascript", "Enter"), "javascript");
  assert.equal(sdkLanguageForKey("python", " "), "python");
  assert.equal(sdkLanguageForKey("python", "Tab"), null);
  assert.equal(sdkLanguageForKey("python", "a"), null);
});

// 开发者 shell 必须把模块级 active language 传入双语言页面，并同时保留两种元数据。
test("developer shell renders javascript sdk as the persisted active language", () => {
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
    sdkExampleStates: { python: pythonState, javascript: javascriptState },
    activeSdkLanguage: "javascript",
  });

  assert.match(container.innerHTML, /data-sdk-language-tab="javascript"/);
  assert.match(container.innerHTML, /aria-selected="true"/);
  assert.match(container.innerHTML, /下载 JavaScript 示例/);
  assert.match(container.innerHTML, /company-embedding/);
});
