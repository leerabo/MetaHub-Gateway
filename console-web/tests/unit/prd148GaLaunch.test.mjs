import assert from "node:assert/strict";
import test from "node:test";
import { renderGaLaunchGates } from "../../src/features/launch-gates/renderGaLaunchGates.js";

const gates = [
  { ga_gate_id: "gag_" + "a".repeat(32), gate_code: "long_stability" },
  { ga_gate_id: "gag_" + "b".repeat(32), gate_code: "tenant_isolation" },
];

test("GA projection gives a human next step and keeps internal identifiers out of the UI", () => {
  const html = renderGaLaunchGates({ gates, candidate: null, launch: null });
  assert.match(html, /GA 发布状态/u);
  assert.match(html, /等待 RC 形成可发布候选/u);
  assert.match(html, /72 小时/u);
  assert.match(html, /双人复核/u);
  assert.doesNotMatch(html, /lvc_[0-9a-f]{32}|gag_[0-9a-f]{32}/u);
});

test("GA launched state points to post-launch review", () => {
  const html = renderGaLaunchGates({ gates: [], candidate: null, launch: { status: "launched", evidence: [] } });
  assert.match(html, /发布已完成，等待发布后复盘/u);
  assert.match(html, /已发布/u);
});

test("candidate-stage GA evidence advances to the next gate before launch exists", () => {
  const candidate = { launch_candidate_id: "lvc_" + "c".repeat(32), revision: 3 };
  const html = renderGaLaunchGates({
    gates,
    candidate,
    evidence: [{ ga_gate_id: gates[0].ga_gate_id }],
    launch: null,
  });
  assert.match(html, /租户隔离/u);
  assert.doesNotMatch(html, /提交下一项 GA 证据：72 小时稳定性/u);
});
