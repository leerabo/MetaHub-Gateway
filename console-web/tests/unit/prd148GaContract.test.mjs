import assert from "node:assert/strict";
import test from "node:test";
import { getGaGates, LAUNCH_GATE_CODES, LAUNCH_WAVES } from "../../src/lib/launchGovernance.js";
import { renderGaLaunchGates } from "../../src/features/launch-gates/renderGaLaunchGates.js";

const NOW = "2026-09-12T12:00:00Z";
const LATER = "2026-09-13T12:00:00Z";
const id = (prefix, number) => `${prefix}_${number.toString(16).padStart(32, "0")}`;
const sha = "a".repeat(64);

// Field names follow GaGateListResponse/GaEvidenceSummary, including the
// candidate-stage evidence that exists before any ga_launches row.
function projection() {
  const gates = LAUNCH_GATE_CODES.map((code, index) => ({
    ga_gate_id: id("gag", index + 1), gate_code: code, policy_version: 1,
    verifier_code: `${code}-verifier`, verifier_version: 1,
    freshness_seconds: 86400, blocking_severity: "p0", policy_sha256: sha,
  }));
  const candidate = {
    launch_candidate_id: id("lvc", 1), candidate_sha256: sha,
    code_tree_sha256: sha, migration_set_sha256: sha, contract_set_sha256: sha,
    artifact_set_sha256: sha, test_manifest_sha256: sha, gate_policy_set_sha256: sha,
    generation: 1, status: "release_candidate", revision: 3,
    created_at: NOW, updated_at: NOW, completed_at: NOW,
    evidence: [], decisions: [], rollbacks: [], review: null,
    waves: LAUNCH_WAVES.map((wave, index) => ({
      launch_wave_id: id("lvw", index + 1), wave_kind: wave, wave_sequence: index + 1,
      workspace_id: id("wsp", 1), cohort_manifest_sha256: sha, feature_flag_generation: 1,
      migration_plan_sha256: sha, rollback_plan_sha256: sha, observation_seconds: 60,
      status: "completed", observation_deadline_at: NOW, health_watermark_sha256: sha,
      revision: 3, started_at: NOW, completed_at: NOW,
    })),
  };
  return { gates, candidate, evidence: [], launch: null, request_id: "req_ga", trace_id: "trc_ga" };
}

function evidence(gateId = id("gag", 1), number = 1) {
  return {
    ga_evidence_id: id("gae", number), launch_candidate_id: id("lvc", 1),
    ga_gate_id: gateId, source_evidence_id: id("lve", number), outcome: "passed",
    finished_at: NOW, expires_at: LATER, created_at: NOW,
  };
}

function launched(extra = {}) {
  return {
    ga_launch_id: id("gal", 1), launch_candidate_id: id("lvc", 1),
    workspace_id: id("wsp", 1), status: "launched", candidate_generation: 1,
    revision: 1, approved_at: NOW, launched_at: NOW, created_at: NOW, updated_at: NOW,
    evidence: [], decisions: [], rollbacks: [], review: null, ...extra,
  };
}

function reply(t, body) {
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json", "Cache-Control": "private, no-store" },
  }));
}

test("GA client accepts the backend empty-state contract", async (t) => {
  const body = { ...projection(), candidate: null };
  reply(t, body);
  const result = await getGaGates({ controlApiUrl: "http://localhost:8001" });
  assert.deepEqual(result.evidence, []);
  assert.equal(result.candidate, null);
  assert.match(renderGaLaunchGates(result), /等待 RC 形成可发布候选/u);
});

test("GA HTTP response advances the candidate evidence step before launch", async (t) => {
  const body = projection();
  body.evidence = [evidence()];
  reply(t, body);
  const result = await getGaGates({ controlApiUrl: "http://localhost:8001" });
  assert.deepEqual(result.evidence, body.evidence);
  assert.ok(Object.isFrozen(result.evidence));
  const html = renderGaLaunchGates(result, Date.parse(NOW));
  assert.match(html, /1 \/ 20/u);
  assert.match(html, new RegExp(`name="gaGateId" value="${body.gates[1].ga_gate_id}"`, "u"));
});

for (const [name, mutate] of [
  ["unknown field", (body) => { body.unexpected = true; }],
  ["missing evidence", (body) => { delete body.evidence; }],
  ["another candidate", (body) => { body.evidence[0].launch_candidate_id = id("lvc", 2); }],
  ["unknown gate", (body) => { body.evidence[0].ga_gate_id = id("gag", 99); }],
  ["duplicate gate", (body) => { body.evidence.push(evidence(id("gag", 1), 2)); }],
  ["duplicate source", (body) => { body.evidence.push({ ...evidence(id("gag", 2), 2), source_evidence_id: id("lve", 1) }); }],
  ["evidence without candidate", (body) => { body.candidate = null; }],
  ["another launch candidate", (body) => { body.launch = launched({ launch_candidate_id: id("lvc", 2) }); }],
]) {
  test(`GA client rejects ${name}`, async (t) => {
    const body = projection();
    body.evidence = [evidence()];
    mutate(body);
    reply(t, body);
    await assert.rejects(getGaGates({ controlApiUrl: "http://localhost:8001" }),
      (error) => error.code === "launch.response_invalid");
  });
}

test("expired evidence never advertises that a candidate can launch", () => {
  const body = projection();
  body.evidence = body.gates.map((gate, index) => evidence(gate.ga_gate_id, index + 1));
  const fresh = renderGaLaunchGates(body, Date.parse(NOW));
  assert.match(fresh, /data-ga-launch-form/u);
  // Expiry is exclusive, just like the server's julianday(expires_at) > now.
  const expired = renderGaLaunchGates(body, Date.parse(LATER));
  assert.match(expired, /0 \/ 20/u);
  assert.match(expired, /已过期/u);
  assert.doesNotMatch(expired, /data-ga-launch-form/u);
});

test("a newly launched release exposes rollback before completing the review", () => {
  const html = renderGaLaunchGates({ ...projection(), launch: launched() });
  assert.match(html, /data-ga-rollback-request-form/u);
  assert.match(html, /data-ga-review-form/u);
});

test("a parsed GA launch retains both recovery and review actions", async (t) => {
  const body = { ...projection(), launch: launched() };
  reply(t, body);
  const result = await getGaGates({ controlApiUrl: "http://localhost:8001" });
  const html = renderGaLaunchGates(result, Date.parse(NOW));
  assert.match(html, /data-ga-rollback-request-form/u);
  assert.match(html, /data-ga-review-form/u);
});

test("pending rollback exposes independent verification without another request", () => {
  const html = renderGaLaunchGates({ ...projection(), launch: launched({
    status: "rollback_pending",
    rollbacks: [{ ga_rollback_id: id("gar", 1), status: "pending" }],
  }) });
  assert.match(html, /data-ga-rollback-verify-form/u);
  assert.doesNotMatch(html, /data-ga-rollback-request-form|data-ga-review-form/u);
});

test("rolled-back release cannot start another rollback", () => {
  const html = renderGaLaunchGates({ ...projection(), launch: launched({ status: "rolled_back" }) });
  assert.match(html, /data-ga-review-form/u);
  assert.doesNotMatch(html, /data-ga-rollback-request-form/u);
});

test("a collecting candidate and an empty gate registry never show a launch action", () => {
  const body = projection();
  body.candidate.status = "collecting";
  assert.doesNotMatch(renderGaLaunchGates(body), /data-ga-launch-form|data-ga-evidence-form/u);
  body.candidate.status = "release_candidate";
  body.gates = [];
  assert.doesNotMatch(renderGaLaunchGates(body), /data-ga-launch-form/u);
});
