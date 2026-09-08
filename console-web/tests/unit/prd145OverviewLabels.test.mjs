import assert from "node:assert/strict";
import test from "node:test";
import { renderConsoleExperience } from "../../src/features/console-experience/renderConsoleExperience.js";
import { renderWorkspaceConsoleExperience } from "../../src/features/workspace-console-experience/renderWorkspaceConsoleExperience.js";

const sources = {
  status: "状态组件数", observability: "服务目标数", jobs: "平台任务数", capacity: "容量服务记录数",
  channel: "渠道结算单数", customer_success: "客户账号记录数", growth: "增长漏斗快照数",
  branding: "品牌数", provider_engineering: "适配器发布数", commercial_accounting: "平台发票数",
  role_tasks: "空间成员记录数", gateway_health: "Gateway 部署数", provider_health: "探测记录数",
  governance: "DLP 规则数", approvals: "审批事项数", usage_cost: "用量记录数",
  notifications: "通知数", deployment: "部署记录数",
};

function render(sections) {
  return renderConsoleExperience({ status: "ready", overview: { status: "ready", sections } },
    { domain: "workspace", view: "overview" });
}
function section(code, value) {
  return { source_code: code, source_revision: 7, freshness: "fresh",
    metrics: [{ metric_code: `${code}.records`, value, state_code: value ? "ready" : "empty" }] };
}

test("overview labels describe actual record counts without machine-code titles", () => {
  const html = render(Object.keys(sources).map((code, index) => section(code, index)));
  for (const label of Object.values(sources)) assert.ok(html.includes(label), label);
  assert.match(html, /修订 7/u);
  assert.match(html, /暂无记录/u);
  assert.match(html, /已有记录/u);
  assert.doesNotMatch(html, /Revision|Role Tasks|Records|Ready|Empty|已完成任务|健康率/u);
  assert.match(html, /<strong>17<\/strong>/u);
});

test("unknown overview codes use controlled text and cannot inject markup", () => {
  const html = render([section('<img src=x onerror=alert(1)>', null)]);
  assert.doesNotMatch(html, /<img|onerror|alert\(1\)/u);
  assert.match(html, /未识别事项/u);
  assert.match(html, /<strong>-<\/strong>/u);
});

function assistantState(citations) {
  return { status: "ready", assistant: { response: {
    query_type: "provider_summary", answer_code: "assistant.answer.ready", outcome: "answered",
    claims: [], citations,
  } } };
}

test("assistant citations use source labels and current Workspace routes", () => {
  const targets = [
    ["gateway_health", "private-gateways", "workspace.gateway.private-deployments", "Gateway 部署"],
    ["provider_health", "provider-health", "workspace.gateway.provider-health", "Provider 探测"],
    ["role_tasks", "overview", "workspace.home.overview", "空间成员"],
    ["usage_cost", "usage", "workspace.finance.usage", "用量统计"],
    ["governance", "dlp-rules", "workspace.governance.dlp", "治理"],
    ["approvals", "approvals", "workspace.home.approvals", "审批"],
    ["notifications", "overview", "workspace.home.overview", "通知"],
    ["deployment", "deployment-summary", "workspace.advanced.deployment-summary", "部署"],
  ];
  const state = assistantState(targets.map(([source_type, navigation_id]) => ({
    source_type, navigation_id, label_code: `console.source.${source_type}`,
  })));
  const before = JSON.stringify(state);
  const html = renderWorkspaceConsoleExperience(state, "admin-assistant");
  for (const [, , route, title] of targets) {
    assert.ok(html.includes(`href="#${route}"`), route);
    assert.ok(html.includes(title), title);
  }
  assert.doesNotMatch(html, /未识别事项|href="#private-gateways"|href="#deployment-summary"/u);
  assert.equal(JSON.stringify(state), before);
});

test("unmapped Workspace citations are text, while Platform routes remain unchanged", () => {
  const state = assistantState([{ source_type: "status", label_code: "console.source.status",
    navigation_id: "status" }]);
  const workspace = renderWorkspaceConsoleExperience(state, "admin-assistant");
  assert.doesNotMatch(workspace, /href=/u);
  const platform = renderConsoleExperience(state, { domain: "platform", view: "admin-assistant" });
  assert.match(platform, /href="#status"/u);
  assert.doesNotMatch(platform, /未识别事项/u);
});
