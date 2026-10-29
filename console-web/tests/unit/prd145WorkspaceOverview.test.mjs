import assert from "node:assert/strict";
import test from "node:test";
import { renderWorkspaceConsoleExperience } from "../../src/features/workspace-console-experience/renderWorkspaceConsoleExperience.js";
import { renderWorkspaceOverview } from "../../src/features/workspace-console-experience/renderWorkspaceOverview.js";

function section(source, values) {
  return { source_code: source, freshness: "fresh", source_revision: 1,
    metrics: Object.entries(values).map(([code, value]) => ({ metric_code: `${source}.${code}`, value })) };
}

test("organization overview shows resources and separate currency costs", () => {
  const html = renderWorkspaceConsoleExperience({ status: "ready", overview: {
    status: "ready", observed_at: "2026-10-08T10:00:00Z", sections: [
      section("usage_cost", { records: 24, month_requests: 17, month_tokens: 4200, month_failures: 2,
        month_cny_micros: 12500000, month_usd_micros: 250000, month_estimated: 1 }),
      section("approvals", { records: 3, pending: 1 }),
      section("provider_health", { records: 5471, needs_check: 2, models: 6, active_models: 5 }),
      section("role_tasks", { records: 3, projects: 2, applications: 4 }),
    ],
  } }, "overview", "owner", null, { workspaceName: "墨械<深圳>" });
  assert.match(html, /墨械&lt;深圳&gt;/);
  assert.match(html, /请求次数<\/span><strong>17<\/strong>/);
  assert.match(html, /Token 使用量<\/span><strong>4,200<\/strong>/);
  assert.match(html, /成员人数<\/span><strong>3<\/strong>/);
  assert.match(html, /项目<\/span><strong>2<\/strong>/);
  assert.match(html, /应用<\/span><strong>4<\/strong>/);
  assert.match(html, /大模型<\/span><strong>6<\/strong>/);
  assert.match(html, /12\.50/);
  assert.match(html, /0\.25/);
  assert.match(html, /包含 1 笔估算用量/);
  assert.match(html, /待检查模型服务/);
  assert.doesNotMatch(html, /待审批|待处理审批/);
  assert.match(html, /href="#workspace\.gateway\.aliases"/);
  assert.doesNotMatch(html, /数据已更新|5,471/);
});

test("overview hides sources absent from the authorized projection", () => {
  const html = renderWorkspaceConsoleExperience({ status: "ready", overview: {
    status: "ready", sections: [section("usage_cost", { month_requests: 0 })],
  } }, "overview", "viewer");
  assert.doesNotMatch(html, /空间成员|模型路由|待处理审批/);
  assert.match(html, /<strong>0<\/strong>/);
});

test("time range selects matching projected totals without mixing periods", () => {
  const html = renderWorkspaceConsoleExperience({ status: "ready", overviewPeriod: "day", overview: {
    sections: [section("usage_cost", { day_requests: 9, day_tokens: 1000, month_requests: 99,
      day_cny_micros: 0, day_usd_micros: 0, day_estimated: 2, month_estimated: 7 })],
  } }, "overview", "owner");
  assert.match(html, /data-workspace-overview-period="day" aria-pressed="true"/);
  assert.match(html, /请求次数<\/span><strong>9<\/strong>/);
  assert.doesNotMatch(html, /<strong>99<\/strong>/);
  assert.match(html, /今日包含 2 笔估算用量/);
  assert.doesNotMatch(html, /包含 7 笔估算用量/);
});

test("estimate notice does not invent counts or reveal a hidden usage projection", () => {
  for (const count of [undefined, 0, -1, "<img src=x onerror=alert(1)>", 1.5]) {
    const html = renderWorkspaceConsoleExperience({ status: "ready", overview: {
      sections: [section("usage_cost", { month_estimated: count })],
    } }, "overview", "owner");
    assert.doesNotMatch(html, /笔估算用量|<img/);
  }
  const html = renderWorkspaceOverview({ status: "ready", overview: {
    sections: [section("usage_cost", { month_estimated: 9 })],
  } }, { hiddenSources: ["usage_cost"] });
  assert.doesNotMatch(html, /笔估算用量/);
});

test("approval page describes its current read-only behavior", () => {
  const html = renderWorkspaceConsoleExperience({ status: "ready", approvals: { items: [] },
    approvalFilters: {}, approvalPage: 1 }, "approvals", "owner");
  assert.match(html, /目前没有员工手动发起申请的入口/);
  assert.match(html, /暂不提供同意或驳回操作/);
  assert.match(html, /预算解封/);
  assert.match(html, /系统触发需要确认的风险操作后/);
});
