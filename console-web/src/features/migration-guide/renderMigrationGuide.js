/**
 * 模块职责：构造并渲染从 OpenAI-compatible 直连调用迁移到 MetaHub 的工作型指南。
 * 维护边界：只使用后端确认的 Gateway URL 和当前租户 active 配置，不读取密钥、不扫描代码库，也不替代后端权限校验。
 * 来源文件：console-web/src/features/migration-guide/renderMigrationGuide.js
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 指南版本用于把静态内容与验收记录对应起来；它不是租户业务数据，也不在浏览器持久化。
export const MIGRATION_GUIDE_VERSION = "1.0.0";

// 两个专属语言值同时约束 ARIA tabs、代码面板和键盘导航，禁止与 SDK 示例页共享状态。
const MIGRATION_LANGUAGES = Object.freeze(["python", "node"]);

// 错误映射聚焦迁移时最常见的治理分支，处理动作禁止建议绕过预算、DLP 或安全策略。
const ERROR_MAPPINGS = Object.freeze([
  ["鉴权", "401 / gateway.auth_missing 或 gateway.auth_invalid", "否", "改用所选 Environment 的 active Virtual Key"],
  ["上游鉴权", "502 / gateway.upstream_auth_failed", "否", "管理员检查 KeyPool 中 Provider Key 和模型权限"],
  ["上游配额", "402 / gateway.upstream_quota_exceeded", "否", "管理员检查厂商余额、计费与项目配额"],
  ["模型不可用", "404 / gateway.upstream_model_not_found", "否", "核对 provider_model_id 与上游模型权限"],
  ["上游限流", "429 / gateway.upstream_rate_limited", "是", "有限退避或由管理员启用其他健康 Key"],
  ["租户限流", "429 / gateway.rate_limit_exceeded", "是", "降低速率或调整 Virtual Key 限额"],
  ["参数不兼容", "502 / gateway.upstream_request_invalid", "否", "移除厂商专有参数并核对 Adapter 能力"],
  ["超时", "504 / gateway.upstream_timeout", "是", "用 request_id 检查上游延迟和超时设置"],
  ["上游不可用", "502/503 / gateway.upstream_error 或 upstream_unavailable", "是", "检查 Provider 健康、路由和 fallback 记录"],
  ["控制面配置", "403/404/409/503 / environment、alias、model、provider 状态错误", "依 code", "修复控制面配置，不把厂商模型 ID 填入 alias"],
  ["预算或 DLP", "402/403 / gateway.budget_exhausted 或 dlp_blocked", "否", "按治理流程处理，禁止自动回直连绕过策略"],
]);

// Python 直连脚本保留 messages、temperature、stream 和消费逻辑，迁移时只替换配置三元组。
const PYTHON_DIRECT_CODE = `import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["PROVIDER_API_KEY"],
    base_url=os.environ["PROVIDER_BASE_URL"],
)
stream_enabled = os.getenv("STREAM", "false").lower() == "true"
response = client.chat.completions.create(
    model=os.environ["PROVIDER_MODEL"],
    messages=[{"role": "user", "content": "用一句话介绍 MetaHub"}],
    temperature=0.2,
    stream=stream_enabled,
)

if stream_enabled:
    for chunk in response:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
    print()
else:
    print(response.choices[0].message.content)
    print("request_id:", getattr(response, "_request_id", "-"))`;

// Python MetaHub 脚本与直连脚本只有环境变量三元组不同，不包含真实 Key、租户 ID 或厂商专有参数。
const PYTHON_METAHUB_CODE = `import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["METAHUB_VIRTUAL_KEY"],
    base_url=os.environ["METAHUB_BASE_URL"],
)
stream_enabled = os.getenv("STREAM", "false").lower() == "true"
response = client.chat.completions.create(
    model=os.environ["METAHUB_MODEL_ALIAS"],
    messages=[{"role": "user", "content": "用一句话介绍 MetaHub"}],
    temperature=0.2,
    stream=stream_enabled,
)

if stream_enabled:
    for chunk in response:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
    print()
else:
    print(response.choices[0].message.content)
    print("request_id:", getattr(response, "_request_id", "-"))`;

// Node 直连脚本仅适用于 Node.js 22+ 服务端，浏览器端不得保存或使用长期 Provider Key。
const NODE_DIRECT_CODE = `import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.PROVIDER_API_KEY,
  baseURL: process.env.PROVIDER_BASE_URL,
});
const streamEnabled = process.env.STREAM === "true";
const request = {
  model: process.env.PROVIDER_MODEL,
  messages: [{ role: "user", content: "用一句话介绍 MetaHub" }],
  temperature: 0.2,
  stream: streamEnabled,
};

if (streamEnabled) {
  const { data: stream, request_id } = await client.chat.completions.create(request).withResponse();
  console.log("request_id:", request_id);
  for await (const chunk of stream) process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
  process.stdout.write("\\n");
} else {
  const response = await client.chat.completions.create(request);
  console.log(response.choices[0].message.content);
  console.log("request_id:", response._request_id ?? "-");
}`;

// Node MetaHub 脚本沿用同一消费逻辑，并通过 Virtual Key 和模型别名保持上游凭据隔离。
const NODE_METAHUB_CODE = `import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.METAHUB_VIRTUAL_KEY,
  baseURL: process.env.METAHUB_BASE_URL,
});
const streamEnabled = process.env.STREAM === "true";
const request = {
  model: process.env.METAHUB_MODEL_ALIAS,
  messages: [{ role: "user", content: "用一句话介绍 MetaHub" }],
  temperature: 0.2,
  stream: streamEnabled,
};

if (streamEnabled) {
  const { data: stream, request_id } = await client.chat.completions.create(request).withResponse();
  console.log("request_id:", request_id);
  for await (const chunk of stream) process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
  process.stdout.write("\\n");
} else {
  const response = await client.chat.completions.create(request);
  console.log(response.choices[0].message.content);
  console.log("request_id:", response._request_id ?? "-");
}`;

/**
 * 构造只读 MigrationGuide 展示模型，并在这里集中执行可信 URL、Environment 和 alias 的安全筛选。
 * 多个候选时绝不默认选择第一项；调用方只有传入用户明确选择的值，MetaHub 复制能力才会变为可用。
 */
export function buildMigrationGuide({
  consoleShellState,
  environmentState,
  modelAliasesState,
  selectedEnvironmentId = null,
  selectedModelAlias = null,
} = {}) {
  const navigation = consoleShellState?.navigation ?? {};
  const gatewayReady = navigation.gateway_base_url_status === "ready" && Boolean(navigation.gateway_base_url);
  const environments = (environmentState?.environments ?? []).filter((item) => item.status === "active");
  const aliases = (modelAliasesState?.model_aliases ?? []).filter(isEligibleAlias);
  const environmentOptions = environments.map((environment) => ({
    ...environment,
    aliases: aliases.filter((alias) => aliasMatchesEnvironment(alias, environment)),
  }));
  const selectedEnvironment = environmentOptions.find(
    (item) => item.environment_id === selectedEnvironmentId,
  ) ?? null;
  const selectedAlias = selectedEnvironment?.aliases.find(
    (item) => item.alias === selectedModelAlias,
  ) ?? null;

  return {
    id: "direct-provider-to-metahub",
    version: MIGRATION_GUIDE_VERSION,
    status: "active",
    gateway_base_url: gatewayReady ? navigation.gateway_base_url : null,
    gateway_base_url_status: gatewayReady ? "ready" : "configuration_required",
    environment_options: environmentOptions,
    selected_environment_id: selectedEnvironment?.environment_id ?? null,
    selected_model_alias: selectedAlias?.alias ?? null,
    copy_ready: Boolean(gatewayReady && selectedEnvironment && selectedAlias),
  };
}

/**
 * 渲染迁移指南；viewer 即使绕过导航直接调用本函数也不会得到指南内容。
 * 动态 URL、名称、ID 和 alias 均在插入 HTML 前转义，代码块只包含受控静态模板和环境变量名。
 */
export function renderMigrationGuide({ guide, authContext, activeLanguage = "python", eventWarning = null } = {}) {
  if (!guide || !hasPermission(authContext, PERMISSIONS.DEVELOPER_PORTAL_READ)) {
    return "";
  }
  const language = normalizeMigrationLanguage(activeLanguage);
  const selectedEnvironment = guide.environment_options.find(
    (item) => item.environment_id === guide.selected_environment_id,
  ) ?? null;
  const aliases = selectedEnvironment?.aliases ?? [];
  const gatewayLabel = guide.gateway_base_url ?? "等待管理员配置可信 Gateway /v1 地址";
  const metaCommand = guide.copy_ready
    ? `METAHUB_BASE_URL=${guide.gateway_base_url} METAHUB_MODEL_ALIAS=${guide.selected_model_alias}`
    : "完成 Gateway、Environment 和模型别名选择后生成";

  return `
    <section class="panel migration-guide" data-migration-guide-root aria-labelledby="migration-guide-title">
      <header class="migration-heading">
        <div>
          <p class="eyebrow">开发者 / 迁移指南 · v${escapeHtml(guide.version)}</p>
          <h2 id="migration-guide-title">从直连模型厂商迁移到 MetaHub</h2>
        </div>
        <span class="migration-scope-badge">服务端 Chat Completions</span>
      </header>
      <p class="migration-lead">通过替换 base URL、API Key 和模型 ID，把现有 OpenAI-compatible 调用接入统一网关；业务 messages、temperature、stream 和消费逻辑保持不变。</p>
      <div class="migration-notice" data-kind="scope">
        <strong>明确适用范围</strong>
        <p>首版仅保证 Python/Node 服务端 <code>/v1/chat/completions</code> 的非流式与流式迁移。<code>/v1/embeddings</code> 仅提供兼容入口提示；Responses、Assistants、厂商专有 SDK/参数和浏览器直连不属于直接替换范围。</p>
      </div>
      <div class="migration-event-warning" data-migration-event-warning ${eventWarning ? "" : "hidden"} role="status">
        ${eventWarning ? `访问记录暂不可用：${escapeHtml(eventWarning.message ?? "未知错误")} · request_id: ${escapeHtml(eventWarning.requestId ?? "-")}` : ""}
      </div>

      <section class="migration-section" aria-labelledby="migration-config-title">
        <div class="migration-section-heading"><span>01</span><h3 id="migration-config-title">确认目标配置</h3></div>
        <div class="migration-config-grid">
          <div class="migration-config-status" data-status="${guide.gateway_base_url_status}">
            <span>可信 Gateway /v1</span><strong>${escapeHtml(gatewayLabel)}</strong>
            <small>${guide.gateway_base_url_status === "ready" ? "来源：GET /console/navigation" : "配置未通过后端校验，MetaHub 复制已禁用"}</small>
          </div>
          <label>Environment
            <select data-migration-environment-select>
              <option value="">请选择 active Environment</option>
              ${guide.environment_options.map((item) => renderEnvironmentOption(item, guide.selected_environment_id)).join("")}
            </select>
          </label>
          <label>同环境 active 模型别名
            <select data-migration-alias-select ${selectedEnvironment ? "" : "disabled"}>
              <option value="">请选择模型别名</option>
              ${aliases.map((item) => renderAliasOption(item, guide.selected_model_alias)).join("")}
            </select>
          </label>
        </div>
        ${renderConfigurationHint(guide, selectedEnvironment)}
      </section>

      <section class="migration-section" aria-labelledby="migration-check-title">
        <div class="migration-section-heading"><span>02</span><h3 id="migration-check-title">迁移前检查</h3></div>
        <ul class="migration-checklist">
          <li>上游 Endpoint 已由管理员注册，Provider Key 只进入控制面 KeyPool。</li>
          <li>模型已进入目录，Environment、模型、Provider 和模型别名均为 active。</li>
          <li>已准备与所选 Environment 一致的 active Virtual Key；页面不接收密钥明文。</li>
          <li>已移除厂商专有字段，或确认对应 Adapter 明确支持。</li>
        </ul>
      </section>

      <section class="migration-section" aria-labelledby="migration-replace-title">
        <div class="migration-section-heading"><span>03</span><h3 id="migration-replace-title">只替换三项配置</h3></div>
        <div class="migration-table-wrap"><table class="migration-table"><thead><tr><th>配置</th><th>直连值</th><th>MetaHub 值</th><th>安全边界</th></tr></thead><tbody>
          <tr><td>base URL</td><td><code>PROVIDER_BASE_URL</code></td><td><code>METAHUB_BASE_URL</code><small>${escapeHtml(gatewayLabel)}</small></td><td>只信任导航响应，不回退 localhost</td></tr>
          <tr><td>API Key</td><td><code>PROVIDER_API_KEY</code></td><td><code>METAHUB_VIRTUAL_KEY</code></td><td>两套凭据隔离，不写入源码、事件或日志</td></tr>
          <tr><td>模型</td><td><code>PROVIDER_MODEL</code></td><td><code>METAHUB_MODEL_ALIAS</code><small>${escapeHtml(guide.selected_model_alias ?? "待选择")}</small></td><td>只能使用所选 Environment 的 active alias</td></tr>
        </tbody></table></div>
      </section>

      <section class="migration-section" aria-labelledby="migration-code-title">
        <div class="migration-section-heading"><span>04</span><h3 id="migration-code-title">完整代码对照</h3></div>
        <div class="migration-tabs" role="tablist" aria-label="迁移示例语言">
          ${renderLanguageTab("python", "Python", language)}
          ${renderLanguageTab("node", "Node.js", language)}
        </div>
        ${renderLanguagePanel({ language: "python", activeLanguage: language, installCommand: "python -m pip install 'openai>=1.40,<2'", directCode: PYTHON_DIRECT_CODE, metaCode: PYTHON_METAHUB_CODE, metaCommand, copyReady: guide.copy_ready })}
        ${renderLanguagePanel({ language: "node", activeLanguage: language, installCommand: "npm install openai@6.47.0  # Node.js 22+ 服务端", directCode: NODE_DIRECT_CODE, metaCode: NODE_METAHUB_CODE, metaCommand, copyReady: guide.copy_ready })}
      </section>

      <section class="migration-section" aria-labelledby="migration-error-title">
        <div class="migration-section-heading"><span>05</span><h3 id="migration-error-title">错误对照与流式 SSE</h3></div>
        <div class="migration-table-wrap"><table class="migration-table migration-error-table"><thead><tr><th>分类</th><th>MetaHub HTTP / code</th><th>可重试</th><th>处理动作</th></tr></thead><tbody>${ERROR_MAPPINGS.map(renderErrorRow).join("")}</tbody></table></div>
        <div class="migration-notice" data-kind="stream"><strong>流式错误必须解析 SSE</strong><p>握手前失败返回普通 JSON 和对应 HTTP 状态；响应头发出后仍可能保持 HTTP 200，并在流中发送 <code>event: error</code>。客户端必须处理 <code>gateway.upstream_stream_timeout</code>、<code>gateway.client_disconnected</code> 和 <code>gateway.upstream_unavailable</code>，不能只检查握手状态。</p></div>
      </section>

      <section class="migration-section" aria-labelledby="migration-verify-title">
        <div class="migration-section-heading"><span>06</span><h3 id="migration-verify-title">验证清单</h3></div>
        <ol class="migration-steps"><li>先运行非流式 Demo，保存 SDK <code>_request_id</code> 或响应头中的 request_id。</li><li>在调用日志核对模型别名、实际 Provider、tokens 和成本。</li><li>设置 <code>STREAM=true</code> 验证流式输出；Node 使用 <code>.withResponse().request_id</code> 保存握手标识。</li><li>主动验证握手后 SSE <code>event: error</code> 分支，不把 HTTP 200 当作最终成功。</li></ol>
      </section>

      <section class="migration-section" aria-labelledby="migration-rollback-title">
        <div class="migration-section-heading"><span>07</span><h3 id="migration-rollback-title">治理、安全与回滚矩阵</h3></div>
        <div class="migration-table-wrap"><table class="migration-table"><thead><tr><th>场景</th><th>是否允许回直连</th><th>前置条件</th></tr></thead><tbody>
          <tr><td>接入配置错误 / 兼容性回归</td><td>审批后允许</td><td>等价治理或紧急例外、canary、流量排空、日志保留</td></tr>
          <tr><td>MetaHub 技术故障</td><td>审批后允许</td><td>确认故障影响，保留 request_id 和回滚审计</td></tr>
          <tr><td>DLP / 预算 / 限流 / 安全策略阻断</td><td><strong>禁止自动绕过</strong></td><td>按治理流程修复策略或申请审批</td></tr>
        </tbody></table></div>
        <div class="migration-notice" data-kind="rollback"><strong>原子切换完整配置</strong><p>部署使用 <code>LLM_ROUTE=direct|metahub</code> 一次选择完整配置对象，不能依次改写三个共享变量。稳定后再决定是否禁用 Virtual Key；Provider Key 永不进入 MetaHub Virtual Key 字段、浏览器或日志。</p></div>
      </section>

      <footer class="migration-boundary">本页面不扫描代码库、不上传源码、不自动改写项目，也不保存 Provider Key、Virtual Key、Prompt 或响应正文。</footer>
    </section>
  `;
}

/**
 * 归一化迁移指南语言，未知或被篡改的 data 值统一回退 Python，确保始终只有一个选中 tab。
 */
export function normalizeMigrationLanguage(language) {
  return MIGRATION_LANGUAGES.includes(language) ? language : "python";
}

/**
 * 把专属 tabs 键盘输入映射到目标语言；未识别按键返回 null，以保留浏览器默认 Tab 行为。
 */
export function migrationLanguageForKey(currentLanguage, key) {
  const current = normalizeMigrationLanguage(currentLanguage);
  const index = MIGRATION_LANGUAGES.indexOf(current);
  if (["ArrowRight", "ArrowDown"].includes(key)) return MIGRATION_LANGUAGES[(index + 1) % MIGRATION_LANGUAGES.length];
  if (["ArrowLeft", "ArrowUp"].includes(key)) return MIGRATION_LANGUAGES[(index - 1 + MIGRATION_LANGUAGES.length) % MIGRATION_LANGUAGES.length];
  if (key === "Home") return MIGRATION_LANGUAGES[0];
  if (key === "End") return MIGRATION_LANGUAGES[MIGRATION_LANGUAGES.length - 1];
  if (["Enter", " "].includes(key)) return current;
  return null;
}

/**
 * alias 必须同时满足自身、所属 Environment、默认模型和默认 Provider 均为 active，缺失状态按不可用处理。
 */
function isEligibleAlias(alias) {
  return alias?.status === "active"
    && alias.environment_status === "active"
    && alias.default_model_status === "active"
    && alias.default_provider_status === "active";
}

/**
 * 优先按稳定 environment_id 匹配；兼容字段只允许与当前 active Environment code 精确相等，禁止跨环境回退。
 */
function aliasMatchesEnvironment(alias, environment) {
  return alias.environment_id
    ? alias.environment_id === environment.environment_id
    : alias.environment === environment.code;
}

/**
 * 渲染 Environment 选项，并把候选 alias 数量作为工作提示；所有后端文本均执行属性转义。
 */
function renderEnvironmentOption(environment, selectedEnvironmentId) {
  const selected = environment.environment_id === selectedEnvironmentId ? " selected" : "";
  return `<option value="${escapeAttribute(environment.environment_id)}"${selected}>${escapeHtml(environment.name ?? environment.code)} / ${escapeHtml(environment.code)}（${environment.aliases.length} 个可用别名）</option>`;
}

/**
 * 渲染当前 Environment 内的 active alias 选项，不把模型 ID 或 Provider Key 伪装成可选别名。
 */
function renderAliasOption(alias, selectedAlias) {
  const selected = alias.alias === selectedAlias ? " selected" : "";
  return `<option value="${escapeAttribute(alias.alias)}"${selected}>${escapeHtml(alias.alias)}</option>`;
}

/**
 * 根据配置缺口给出受控跳转提示；不生成自动修复动作，也不回落到第一个 Environment 或 alias。
 */
function renderConfigurationHint(guide, selectedEnvironment) {
  if (guide.gateway_base_url_status !== "ready") {
    return `<p class="migration-config-hint">请管理员修复公开 Gateway URL 配置；在状态变为 ready 前不可复制 MetaHub 代码。</p>`;
  }
  if (guide.environment_options.length === 0) {
    return `<p class="migration-config-hint">没有可用 Environment。请前往 Environment、模型别名和 Provider 健康页面完成 active 配置。</p>`;
  }
  if (!selectedEnvironment) {
    return `<p class="migration-config-hint">必须显式选择 Environment；多候选时不会静默选择第一项。</p>`;
  }
  if (selectedEnvironment.aliases.length === 0) {
    return `<p class="migration-config-hint">该 Environment 没有同时满足 alias、模型和 Provider active 的候选，请检查模型别名与 Provider 健康。</p>`;
  }
  // Environment 已确认后仍要求用户显式选择同环境 alias，不能因当前只有一个候选而自动填充。
  if (!guide.selected_model_alias) {
    return `<p class="migration-config-hint">请选择同 Environment 的 active 模型别名后再复制 MetaHub 代码。</p>`;
  }
  // 三项配置就绪只代表示例可复制，Virtual Key 明文仍不得进入页面输入框或生成的代码文本。
  return `<p class="migration-config-hint migration-config-ready">配置三元组已就绪；Virtual Key 仍只通过部署环境变量注入。</p>`;
}

/**
 * 渲染一个专属 ARIA tab，ID 与 tabpanel 一一对应并使用 roving tabindex。
 */
function renderLanguageTab(language, label, activeLanguage) {
  const selected = language === activeLanguage;
  return `<button type="button" role="tab" id="migration-${language}-tab" aria-controls="migration-${language}-panel" aria-selected="${selected}" tabindex="${selected ? "0" : "-1"}" data-migration-language-tab="${language}">${label}</button>`;
}

/**
 * 渲染单个语言面板和 direct/metahub 两列代码；每个代码块都有唯一 target 与独立反馈节点。
 */
function renderLanguagePanel({ language, activeLanguage, installCommand, directCode, metaCode, metaCommand, copyReady }) {
  const hidden = language === activeLanguage ? "" : " hidden";
  return `<div role="tabpanel" id="migration-${language}-panel" aria-labelledby="migration-${language}-tab" data-migration-language-panel="${language}"${hidden}>
    <p class="migration-install"><strong>安装：</strong><code>${escapeHtml(installCommand)}</code></p>
    <div class="migration-code-grid">
      ${renderCodeBlock({ id: `${language}-direct`, title: "直连模型厂商", command: "使用 PROVIDER_* 三元组", code: directCode, disabled: false })}
      ${renderCodeBlock({ id: `${language}-metahub`, title: "切换 MetaHub", command: metaCommand, code: metaCode, disabled: !copyReady })}
    </div>
  </div>`;
}

/**
 * 渲染单个可复制代码块；复制按钮只引用同块 pre ID，反馈节点不在不同语言或路由之间共享。
 */
function renderCodeBlock({ id, title, command, code, disabled }) {
  return `<article class="migration-code-block" data-migration-code-block="${escapeAttribute(id)}">
    <header><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(command)}</small></div><button type="button" data-migration-copy-target="migration-${escapeAttribute(id)}-code" aria-describedby="migration-${escapeAttribute(id)}-feedback"${disabled ? " disabled" : ""}>复制</button></header>
    <pre id="migration-${escapeAttribute(id)}-code"><code>${escapeHtml(code)}</code></pre>
    <p id="migration-${escapeAttribute(id)}-feedback" class="migration-copy-feedback" role="status"></p>
  </article>`;
}

/**
 * 渲染错误映射行，静态常量仍统一走 HTML 转义，避免后续内容维护引入未编码文本。
 */
function renderErrorRow([category, code, retryable, action]) {
  return `<tr><td>${escapeHtml(category)}</td><td><code>${escapeHtml(code)}</code></td><td>${escapeHtml(retryable)}</td><td>${escapeHtml(action)}</td></tr>`;
}

/**
 * 对文本节点执行 HTML 转义，阻断后端名称、URL 和 alias 注入标签或脚本。
 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * 对属性上下文补充反引号转义，data/value/id 均不能接收未经编码的动态文本。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
