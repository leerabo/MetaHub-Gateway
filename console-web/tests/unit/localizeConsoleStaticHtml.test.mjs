import test from "node:test";
import assert from "node:assert/strict";
import { createI18n } from "../../src/i18n/messages.js";
import { localizeConsoleStaticHtml } from "../../src/i18n/localizeConsoleStaticHtml.js";

test("projects known static console labels for English sessions", () => {
  const html = '<h2>模型目录</h2><p>正在加载模型目录...</p><span>组织名称</span>';
  const localized = localizeConsoleStaticHtml(html, createI18n("en-US"));
  assert.equal(localized, '<h2>Model catalog</h2><p>Loading model catalog...</p><span>Organization name</span>');
});

test("leaves Chinese default and user data untouched", () => {
  const html = '<h2>模型目录</h2><span>客户的模型目录</span>';
  assert.equal(localizeConsoleStaticHtml(html, createI18n("zh-CN")), html);
  assert.equal(
    localizeConsoleStaticHtml('<span>Acme 模型目录</span>', createI18n("en-US")),
    '<span>Acme Model catalog</span>',
  );
});

test("keeps the Chinese surface free of legacy English renderer literals", () => {
  const html = '<h2>Model catalog</h2><span>Model routing</span><label>Status</label><span>cost rates</span><span>Prompt 模板</span><code data-model="Model catalog">Acme Model catalog</code>';
  const localized = localizeConsoleStaticHtml(html, createI18n("zh-CN"));
  assert.equal(localized, '<h2>模型目录</h2><span>模型路由</span><label>状态</label><span>成本率</span><span>提示词模板</span><code data-model="Model catalog">Acme Model catalog</code>');
  assert.match(localized, /data-model="Model catalog"/u);
});

test("Chinese translation preserves model IDs, provider names, and diagnostic field names", () => {
  const html = '<span>kimi-k2.7-code</span><span>QA Fake Endpoint</span><pre>{"status":"Updated","prompt":"code"}</pre><label>Status</label>';
  assert.equal(
    localizeConsoleStaticHtml(html, createI18n("zh-CN")),
    '<span>kimi-k2.7-code</span><span>QA Fake Endpoint</span><pre>{"status":"Updated","prompt":"code"}</pre><label>状态</label>',
  );
});

test("covers asynchronous support and private gateway panel labels", () => {
  const html = '<h2>客户支持</h2><label>状态</label><h2>私有 Gateway</h2><span>正在加载</span>';
  assert.equal(
    localizeConsoleStaticHtml(html, createI18n("en-US")),
    '<h2>Customer support</h2><label>Status</label><h2>Private Gateways</h2><span>Loading</span>',
  );
});

test("localizes support and private Gateway phrases without mixed fragments", () => {
  const html = `
    <span>工单、SLA 与安全诊断证据</span>
    <option>全部状态</option><option>全部优先级</option>
    <h4>消息时间线</h4><span>暂无升级</span>
    <label>回复客户支持</label><h4>工单状态</h4><span>CAS Rev 1</span>
    <option>P3 中</option><option>集成</option><button>标记已解决</button>
    <h3>私有 Gateway</h3><p>设备身份、交付验证、脱敏支持与运行状态</p>
    <strong>尚未创建私有 Gateway 部署</strong>
    <p>使用右上角“创建部署”，再选择安装环境、运行架构和签名安装介质。</p>
  `;
  const localized = localizeConsoleStaticHtml(html, createI18n("en-US"));
  assert.doesNotMatch(localized, /[\u4e00-\u9fff]/u);
  assert.doesNotMatch(localized, /全部Status|消息Time线|Customer support|None yet升级/u);
  assert.match(localized, /Tickets, SLAs, and security diagnostic evidence/u);
  assert.match(localized, /No Private Gateway deployment yet/u);
  assert.equal(localizeConsoleStaticHtml(html, createI18n("zh-CN")), html);
});

test("cleans mixed task-home fallback phrases in English sessions", () => {
  const html = localizeConsoleStaticHtml(
    '<h3>Connect a model 模型服务</h3><p>模型服务 and service credential</p><button>Configure 模型服务</button><button>Configure 模型</button>',
    createI18n("en-US"),
  );
  assert.equal(
    html,
    '<h3>Connect a model provider</h3><p>Provider and service credential</p><button>Configure provider</button><button>Configure models</button>',
  );
  assert.doesNotMatch(html, /模型服务|模型(?=<\/button>)/u);
});

test("cleans task-home phrases split across static inline nodes", () => {
  const html = '<h3>Connect a model <span>模型服务</span></h3><p><span>模型服务</span> and <em>service credential</em></p><button>Configure <span>模型服务</span></button>';
  assert.equal(
    localizeConsoleStaticHtml(html, createI18n("en-US")),
    '<h3>Connect a model provider</h3><p>Provider and service credential</p><button>Configure provider</button>',
  );
  assert.equal(
    localizeConsoleStaticHtml(html, createI18n("zh-CN")),
    '<h3>连接模型服务</h3><p>模型服务和服务凭据</p><button>配置模型服务</button>',
  );
});

test("projects member and provider controls without bilingual fragments", () => {
  const html = localizeConsoleStaticHtml(
    '<h2>Model providers</h2><button>Add model 模型服务</button><summary>自定义接入（中转站 / OpenAI 兼容地址）</summary><p>可直接填写任意自定义中转站或兼容接口，不限于下面的品牌目录。</p><button>测试连接</button><button>管理服务</button><button>编辑</button><button>禁用</button><button>删除服务</button><span>默认模型</span><span>1 个模型服务</span><span>1 模型服务(s)</span><span>1 个可用 KEY</span><span>成员</span><label>搜索成员</label><input placeholder="姓名或邮箱"><button>搜索</button><button>清除</button><span>只读成员</span><span>Read-only成员</span><button>移除</button>',
    createI18n("en-US"),
  );
  assert.equal(
    html,
    '<h2>Model providers</h2><button>Add model provider</button><summary>Custom connection (relay / OpenAI-compatible URL)</summary><p>Enter any custom relay or compatible endpoint; the brand directory is optional.</p><button>Test connection</button><button>Manage service</button><button>Edit</button><button>Disable</button><button>Delete provider</button><span>Default model</span><span>1 provider</span><span>1 provider(s)</span><span>1 active key(s)</span><span>Members</span><label>Search members</label><input placeholder="Name or email"><button>Search</button><button>Clear</button><span>Read-only member</span><span>Read-only member</span><button>Remove</button>',
  );
  assert.doesNotMatch(html, /[\u3400-\u9fff]/u);
});

test("normalizes counters after generic English noun projection", () => {
  const html = localizeConsoleStaticHtml(
    '<span>3 位成员</span><span>3 位Members</span><span>1 把可用 KEY</span><span>1 把active key(s)</span>',
    createI18n("en-US"),
  );
  assert.equal(
    html,
    '<span>3 members</span><span>3 members</span><span>1 active key(s)</span><span>1 active key(s)</span>',
  );
  assert.doesNotMatch(html, /[\u4e00-\u9fff]/u);
});

test("projects budget panel phrases without mixed Chinese fragments", () => {
  const html = `
    <h2>预算中心</h2>
    <span>0 条规则</span>
    <strong>未配置</strong>
    <span>当前状态</span><span>有效动作</span><span>限制层级</span>
    <span>设置日预算</span><span>保存预算</span>
    <span>今日状态</span><span>更新时间</span>
    <span>当前 Environment 尚无可展示的预算作用链。</span>
    <span>尚未配置租户、Project 或 Virtual Key 日预算。</span>
    <span>环境</span><span>租户</span><span>层级</span><option value="tenant">租户</option>
  `;
  const localized = localizeConsoleStaticHtml(html, createI18n("en-US"));
  assert.doesNotMatch(localized, /[\u4e00-\u9fff]/u);
  assert.match(localized, /0 rule\(s\)/u);
  assert.match(localized, /No tenant, Project, or Virtual Key daily budget configured\./u);
  assert.equal(localizeConsoleStaticHtml(html, createI18n("zh-CN")), html);
});

test("localizes complete budget and routing sentences before generic tokens", () => {
  const html = [
    "<span>选择调用 KEY</span>",
    "<span>尚未配置租户、项目或调用 KEY 日预算。</span>",
    "<p>查看每个模型别名的主备模型和默认路由方式。</p>",
    "<span>0 条策略</span>",
    "<span>模型别名 / 运行环境</span><span>配置候选</span><span>当前模式 / 版本</span>",
    "<span>当前组织还没有模型别名。</span>",
  ].join("");
  const localized = localizeConsoleStaticHtml(html, createI18n("en-US"));
  assert.equal(
    localized,
    "<span>Select a calling key</span><span>No tenant, project, or calling key daily budget configured.</span><p>Review the primary and fallback models and default routing mode for each model alias.</p><span>0 policy(s)</span><span>Model alias / environment</span><span>Configured candidates</span><span>Current mode / version</span><span>This organization has no model aliases yet.</span>",
  );
  assert.doesNotMatch(localized, /[\u4e00-\u9fff]/u);
  assert.equal(localizeConsoleStaticHtml(html, createI18n("zh-CN")), html);
});

test("projects API documentation and empty contract states as one language", () => {
  const html = [
    "<h2>API 文档 · 统一模型接口</h2>",
    "<p>所有工具共用一个网关地址和个人 KEY；按工具要求选择协议。</p>",
    "<strong>模型列表（自动发现）</strong>",
    "<p>这些路径属于同一个网关，共用个人 KEY。</p>",
    "<h2>API 版本与 SDK</h2><h3>暂无已发布内容</h3>",
    "<p>当前还没有可供组织成员使用的 API 版本、SDK 或 Webhook 契约。发布后，版本、安全检查和更新记录会显示在这里。</p>",
  ].join("");
  const localized = localizeConsoleStaticHtml(html, createI18n("en-US"));
  assert.doesNotMatch(localized, /[\u4e00-\u9fff]/u);
  assert.match(localized, /All tools use one Gateway URL and a personal key/u);
  assert.match(localized, /No API versions, SDKs, or Webhook contracts are available/u);
  assert.equal(localizeConsoleStaticHtml(html, createI18n("zh-CN")), html);
});

test("projects the model quality panel without mixed-language fragments", () => {
  const html = `
    <h2>模型评分</h2>
    <p>当前租户模型的成功率、延迟和人工质量先验。</p>
    <label for="model-quality-model-id"><span>模型 ID</span><input id="model-quality-model-id" /></label>
    <button>查询</button><button>清空</button>
    <span>模型 / Provider</span><span>调用样本</span><span>成功 / 错误</span>
    <span>平均延迟</span><span>人工 / 有效评分</span><span>逐请求反馈</span><span>版本</span>
    <span>0 <em>暂无观测</em></span><span>未设置</span><span>5 个样本</span>
    <span>12 条 / 已采纳 9 / 75%</span><small>分布 1:0 / 2:1 / 3:2 / 4:3 / 5:6 / v12</small>
    <span>反馈 v1</span><span>统计 v3</span><span>人工评分（%）</span>
    <button>保存</button><button>清除评分</button><button>取消</button>
    <button>上一页</button><button>下一页</button>
  `;
  const localized = localizeConsoleStaticHtml(html, createI18n("en-US"));
  assert.doesNotMatch(localized, /[\u4e00-\u9fff]/u);
  assert.match(localized, /Current workspace model success rates, latency, and human quality priors\./u);
  assert.match(localized, /No observations yet/u);
  assert.match(localized, /Not set/u);
  assert.doesNotMatch(localized, /None yet观测|未Settings|延迟|人工/u);
});

test("projects the model price page without mixed Chinese and English fragments", () => {
  const html = `
    <p>组织成本</p><h1>模型服务价格与成本记录</h1>
    <summary><span>筛选成本率</span><small>按模型服务、模型、币种或状态查找</small></summary>
    <th>模型服务 / 模型</th><th>输入成本</th><th>输出成本</th>
    <summary><span>新增模型服务价格</span><small>组织所有者 · 只在需要调整组织成本时填写</small></summary>
    <label>输入价格 / 千 Token</label><button>确认停用</button>
  `;
  const localized = localizeConsoleStaticHtml(html, createI18n("en-US"));
  assert.doesNotMatch(localized, /[\u4e00-\u9fff]/u);
  assert.match(localized, /Organization costs/u);
  assert.match(localized, /Model provider prices and cost records/u);
  assert.match(localized, /Find by provider, model, currency, or status/u);
  assert.equal(localizeConsoleStaticHtml(html, createI18n("zh-CN")), html);
});

test("localizes short finance and usage labels without rewriting user data", () => {
  const html = [
    "<span>用量与价格</span><span>额度</span><span>预算</span><span>来源</span>",
    "<span>历史模型基线</span><span>筛选用量记录</span><span>按需筛选，默认显示全部记录</span><span>请求次数</span>",
    "<span>模型</span><span>令牌</span><span>成本</span><span>真实</span>",
    "<span>请求数</span><span>0</span><span>条</span><span>真实 0</span><span>输入 Token</span><span>输出 Token</span>",
    "<span>Acme 模型</span><span data-model=\"模型\">模型</span>",
  ].join("");
  const localized = localizeConsoleStaticHtml(html, createI18n("en-US"));
  assert.match(localized, /Usage and pricing.*Quotas.*Budgets.*Source/u);
  assert.match(localized, /Historical model baseline.*Filter usage records.*Requests/u);
  assert.match(localized, /Filter as needed; all records shown by default/u);
  assert.match(localized, /Requests.*0.*records.*Real 0.*Input tokens.*Output tokens/u);
  assert.match(localized, /Model.*Tokens.*Cost.*Real/u);
  assert.match(localized, /Acme 模型/u);
  assert.match(localized, /data-model="模型"/u);
  assert.equal(localizeConsoleStaticHtml(html, createI18n("zh-CN")), html);
});
