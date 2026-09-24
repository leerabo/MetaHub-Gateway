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
