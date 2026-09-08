import assert from "node:assert/strict";
import test from "node:test";
import { renderConsoleExperience } from "../../src/features/console-experience/renderConsoleExperience.js";

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
