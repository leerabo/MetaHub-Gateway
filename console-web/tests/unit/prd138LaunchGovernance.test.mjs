import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderLaunchGates } from "../../src/features/launch-gates/renderLaunchGates.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  LAUNCH_ACTORS,
  LAUNCH_GATE_CODES,
  LAUNCH_WAVES,
  commandLaunchWave,
  createLaunchCandidate,
  decideLaunchCandidate,
  getLaunchGates,
  launchArtifactSetSha256,
  requestLaunchRollback,
  reviewLaunchCandidate,
  submitLaunchEvidence,
  verifyLaunchRollback,
} from "../../src/lib/launchGovernance.js";
import { PlatformApiError } from "../../src/lib/platform.js";

const CONTROL_URL = "http://control.local";
const CANDIDATE_ID = `lvc_${"1".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"2".repeat(32)}`;
const CSRF = "C".repeat(43);
const STEP_UP = "G".repeat(43);
const SIGNATURE = "S".repeat(64);
const IDEMPOTENCY = "launch-governance-command-0001";
const NOW = "2026-08-31T08:00:00.000Z";
const LATER = "2026-09-01T08:00:00.000Z";

test("PRD-138 clients keep four fixed Cookie routes and canonical candidate digest", async (t) => {
  installDocument(t);
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/platform/launch-gates") return jsonResponse(gateListResponse());
    if (path.startsWith("/platform/launch-gates/") && path.endsWith("/evidence")) {
      const body = JSON.parse(options.body);
      const proof = evidence({
        launch_gate_id: body.launch_gate_id ?? gates()[0].launch_gate_id,
        gate_code: LAUNCH_GATE_CODES[0],
        workspace_id: body.workspace_id,
        artifact_sha256: body.artifact_sha256,
        verifier_code: body.verifier_code,
        outcome: body.outcome,
        metric_value: body.metric_value,
        metric_unit: body.metric_unit,
      });
      return jsonResponse(evidenceResponse(proof), 201);
    }
    if (path === "/platform/launches") {
      const body = JSON.parse(options.body);
      if (body.operation === "create") {
        return jsonResponse(launchResponse(candidate({
          code_tree_sha256: body.code_tree_sha256,
          migration_set_sha256: body.migration_set_sha256,
          contract_set_sha256: body.contract_set_sha256,
          artifact_set_sha256: body.artifact_set_sha256,
          test_manifest_sha256: body.test_manifest_sha256,
        })), 201);
      }
      if (body.operation === "decide") {
        return jsonResponse(launchResponse(candidate({
          status: "signing",
          decisions: [decision({
            actor_kind: body.actor_kind,
            decision: body.approved ? "approved" : "rejected",
            reason_code: body.reason_code,
          })],
        })), 201);
      }
      if (body.operation === "start_wave") {
        return jsonResponse(launchResponse(candidate({
          status: "launching",
          waves: waveList({ internal: { status: "running", started_at: NOW,
            observation_deadline_at: LATER } }),
        })), 201);
      }
      if (body.operation === "review") {
        return jsonResponse(launchResponse(candidate({
          status: "release_candidate",
          review: review({
            summary_sha256: body.summary_sha256,
            action_items_sha256: body.action_items_sha256,
          }),
        })), 201);
      }
    }
    if (path === `/platform/launches/${CANDIDATE_ID}/rollback`) {
      const body = JSON.parse(options.body);
      const record = rollback(body.operation === "request" ? {
        from_wave_kind: body.from_wave_kind,
        to_wave_kind: body.to_wave_kind,
        trigger_code: body.trigger_code,
      } : {
        launch_rollback_id: body.launch_rollback_id,
        status: body.outcome,
        verification_sha256: body.verification_sha256,
        verified_at: NOW,
      });
      return jsonResponse(rollbackResponse(record, body.operation === "request"
        ? "rollback_pending" : "rolled_back"), 201);
    }
    throw new Error(`unexpected route ${path}`);
  });

  const artifacts = artifactInputs();
  const expectedArtifactSet = createHash("sha256").update(JSON.stringify(
    artifacts.map((item) => ({
      artifact_sha256: item.artifact_sha256,
      gate_code: item.gate_code,
      workspace_id: item.workspace_id,
    })).sort((left, right) => left.gate_code.localeCompare(right.gate_code)),
  )).digest("hex");
  assert.equal(await launchArtifactSetSha256(artifacts), expectedArtifactSet);
  const listed = await getLaunchGates({ controlApiUrl: CONTROL_URL });
  const created = await createLaunchCandidate({
    controlApiUrl: CONTROL_URL,
    codeTreeSha256: hex(100),
    migrationSetSha256: hex(101),
    contractSetSha256: hex(102),
    testManifestSha256: hex(103),
    artifacts,
    waves: waveInputs(),
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  const gate = listed.gates[0];
  const submitted = await submitLaunchEvidence({
    controlApiUrl: CONTROL_URL,
    launchGateId: gate.launch_gate_id,
    launchCandidateId: CANDIDATE_ID,
    expectedRevision: 1,
    workspaceId: WORKSPACE_ID,
    artifactSha256: artifacts[0].artifact_sha256,
    verifierCode: gate.verifier_code,
    outcome: "passed",
    metricValue: 1,
    metricUnit: "pass_ratio",
    environmentSha256: hex(104),
    provenanceSha256: hex(105),
    startedAt: NOW,
    finishedAt: LATER,
    signature: SIGNATURE,
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  const decided = await decideLaunchCandidate({
    controlApiUrl: CONTROL_URL,
    launchCandidateId: CANDIDATE_ID,
    expectedRevision: 2,
    actorKind: "product",
    approved: true,
    reasonCode: "approved_release",
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  const started = await commandLaunchWave({
    controlApiUrl: CONTROL_URL,
    operation: "start_wave",
    launchCandidateId: CANDIDATE_ID,
    expectedRevision: 3,
    waveKind: "internal",
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  const reviewed = await reviewLaunchCandidate({
    controlApiUrl: CONTROL_URL,
    launchCandidateId: CANDIDATE_ID,
    expectedRevision: 4,
    summarySha256: hex(106),
    actionItemsSha256: hex(107),
    conclusion: "release_candidate",
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  const requested = await requestLaunchRollback({
    controlApiUrl: CONTROL_URL,
    launchCandidateId: CANDIDATE_ID,
    expectedRevision: 5,
    fromWaveKind: "internal",
    toWaveKind: "pre_launch",
    triggerCode: "operator_request",
    triggerSha256: hex(108),
    checkpointSha256: hex(109),
    jobReceiptSha256: hex(110),
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  const verified = await verifyLaunchRollback({
    controlApiUrl: CONTROL_URL,
    launchCandidateId: CANDIDATE_ID,
    expectedRevision: 1,
    launchRollbackId: requested.rollback.launch_rollback_id,
    outcome: "passed",
    verificationSha256: hex(111),
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });

  assert.equal(created.candidate.artifact_set_sha256, expectedArtifactSet);
  assert.equal(submitted.evidence.launch_gate_id, gate.launch_gate_id);
  assert.equal(decided.candidate.decisions[0].actor_kind, "product");
  assert.equal(started.candidate.waves[0].status, "running");
  assert.equal(reviewed.candidate.review.conclusion, "release_candidate");
  assert.equal(verified.candidate.status, "rolled_back");
  assert.deepEqual([...new Set(calls.map(({ url }) => new URL(url).pathname))], [
    "/platform/launch-gates",
    "/platform/launches",
    `/platform/launch-gates/${gate.launch_gate_id}/evidence`,
    `/platform/launches/${CANDIDATE_ID}/rollback`,
  ]);
  calls.forEach(({ options }) => {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
  });
  const posts = calls.filter(({ options }) => options.method === "POST");
  posts.forEach(({ options }) => {
    assert.equal(options.headers["X-CSRF-Token"], CSRF);
    assert.equal(options.headers["X-Step-Up-Grant"], STEP_UP);
    assert.equal(options.headers["Idempotency-Key"], IDEMPOTENCY);
  });
  const createBody = JSON.parse(calls[1].options.body);
  assert.equal(createBody.artifact_set_sha256, expectedArtifactSet);
  assert.equal(createBody.artifacts.length, 20);
  assert.equal(createBody.waves.length, 4);
});

test("PRD-138 clients reject response extensions, owner drift, and GA state", async (t) => {
  installDocument(t);
  installFetch(t, () => jsonResponse({ ...gateListResponse(), endpoint_url: "http://internal" }));
  await assert.rejects(getLaunchGates({ controlApiUrl: CONTROL_URL }), responseInvalid);

  globalThis.fetch = async () => jsonResponse(gateListResponse(candidate({
    status: "general_availability",
  })));
  await assert.rejects(getLaunchGates({ controlApiUrl: CONTROL_URL }), responseInvalid);

  globalThis.fetch = async () => jsonResponse(gateListResponse(candidate({
    evidence: [evidence({ launch_gate_id: gates()[1].launch_gate_id, gate_code: "wb" })],
  })));
  await assert.rejects(getLaunchGates({ controlApiUrl: CONTROL_URL }), responseInvalid);
});

test("PRD-138 renderer exposes dense gate, signer, wave, rollback, and RC states", () => {
  const permissions = new Set([
    "platform.launch.read", "platform.launch.create", "platform.launch.evidence.submit",
    "platform.launch.sign.product", "platform.launch.execute", "platform.launch.rollback",
  ]);
  const collecting = renderLaunchGates(platformState(candidate()), permissions);
  assert.match(collecting, /data-platform-launch-root/u);
  assert.match(collecting, /Gate registry/u);
  assert.match(collecting, /data-platform-launch-create-form/u);
  assert.match(collecting, /data-platform-launch-evidence-form/u);
  assert.match(collecting, /20 policies/u);

  const signing = renderLaunchGates(platformState(candidate({ status: "signing" })), permissions);
  assert.match(signing, /Go \/ no-go decisions/u);
  assert.match(signing, /data-platform-launch-decision-form/u);

  const launching = candidate({
    status: "launching",
    decisions: LAUNCH_ACTORS.map((actor, index) => decision({
      actor_kind: actor,
      operator_id: `pto_${(index + 1).toString(16).padStart(32, "0")}`,
      go_no_go_decision_id: `lgd_${(index + 1).toString(16).padStart(32, "0")}`,
    })),
    waves: waveList({ internal: { status: "running", started_at: NOW,
      observation_deadline_at: LATER } }),
  });
  const root = fakeRoot();
  renderPlatformWorkspace(root, shellState(launching, permissions));
  assert.match(root.innerHTML, /Launch waves/u);
  assert.match(root.innerHTML, /data-platform-launch-wave-form/u);
  assert.match(root.innerHTML, /data-platform-launch-rollback-request-form/u);
  assert.doesNotMatch(root.innerHTML,
    /tenant_id|object_url|artifact_uri|raw_log|Authorization|Bearer|general_availability/iu);

  const reviewHtml = renderLaunchGates(platformState(candidate({
    status: "review_pending",
    waves: waveList(Object.fromEntries(LAUNCH_WAVES.map((wave) => [wave, {
      status: "completed", started_at: NOW, completed_at: LATER,
      health_watermark_sha256: hex(230 + LAUNCH_WAVES.indexOf(wave)),
    }]))),
  })), permissions);
  assert.match(reviewHtml, /data-platform-launch-review-form/u);
  assert.match(reviewHtml, /release candidate conclusion/u);
});

test("PRD-138 controller consumes step-up, reuses retry idempotency, and honors revocation", async (t) => {
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash: "#launch-gates" },
    history: { replaceState() {} },
  };
  t.after(() => { globalThis.window = originalWindow; });
  let permissions = [
    "platform.launch.read", "platform.launch.create", "platform.launch.evidence.submit",
  ];
  let attempts = 0;
  let launchReads = 0;
  const submissions = [];
  const creations = [];
  const grants = [];
  const firstGate = gates()[0];
  const api = {
    session: async () => ({ context: {
      permissions: [...permissions],
      operator: { display_name: "Verifier", roles: ["platform_verification"] },
    } }),
    navigation: async () => ({ items: [{
      id: "launch-gates", label: "Launch gates", href: "/platform.html#launch-gates",
    }] }),
    launchGates: async () => {
      launchReads += 1;
      return gateListResponse();
    },
    createIdentityStepUpGrant: async (input) => {
      grants.push(input);
      return { grant: STEP_UP };
    },
    createLaunchCandidate: async (input) => {
      creations.push(input);
      return launchResponse(candidate());
    },
    submitLaunchEvidence: async (input) => {
      submissions.push(input);
      attempts += 1;
      if (attempts === 1) throw new PlatformApiError({
        status: 503, code: "launch.unavailable", message: "retry",
      });
      return evidenceResponse(evidence());
    },
  };
  const controller = createPlatformPageController({
    root: fakeRoot(),
    api,
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  assert.equal(await controller.mount(), true);
  assert.equal(launchReads, 1);
  controller.selectView("overview");
  controller.selectView("launch-gates");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(launchReads, 2);
  const apiWorkspace = `wsp_${"e".repeat(32)}`;
  assert.equal(await controller.createLaunchCandidate({
    artifacts: [
      { gate_code: "wb", workspace_id: WORKSPACE_ID, artifact_sha256: hex(390) },
      { gate_code: "api", workspace_id: apiWorkspace, artifact_sha256: hex(391) },
    ],
    waves: [],
    confirmed: true,
    method: "totp",
    verificationCode: "123456",
  }), true);
  assert.equal(creations.length, 1);
  const command = {
    launchGateId: firstGate.launch_gate_id,
    launchCandidateId: CANDIDATE_ID,
    expectedRevision: 1,
    workspaceId: WORKSPACE_ID,
    artifactSha256: hex(300),
    verifierCode: firstGate.verifier_code,
    outcome: "passed",
    metricValue: 1,
    metricUnit: "pass_ratio",
    environmentSha256: hex(301),
    provenanceSha256: hex(302),
    startedAt: NOW,
    finishedAt: LATER,
    signature: SIGNATURE,
    method: "totp",
    verificationCode: "123456",
    confirmed: true,
  };
  assert.equal(await controller.submitLaunchEvidence(command), false);
  assert.equal(await controller.submitLaunchEvidence(command), true);
  assert.deepEqual(submissions.map((item) => item.idempotencyKey), [IDEMPOTENCY, IDEMPOTENCY]);
  assert.equal(submissions.every((item) => item.verificationCode === undefined), true);
  assert.deepEqual(grants.map((item) => ({
    actionCode: item.actionCode,
    resourceType: item.resourceType,
    resourceId: item.resourceId,
  })), [
    { actionCode: "platform.launch.create", resourceType: "workspace",
      resourceId: apiWorkspace },
    { actionCode: "platform.launch.evidence.submit", resourceType: "launch_gate",
      resourceId: firstGate.launch_gate_id },
    { actionCode: "platform.launch.evidence.submit", resourceType: "launch_gate",
      resourceId: firstGate.launch_gate_id },
  ]);

  permissions = ["platform.launch.read"];
  assert.equal(await controller.refresh(), true);
  assert.equal(await controller.submitLaunchEvidence(command), false);
  assert.equal(submissions.length, 2);
  controller.destroy();
});

test("PRD-138 sources retain fixed routes, responsive layout, and no secret persistence", async () => {
  const [client, state, renderer, css, routes, models, migration] = await Promise.all([
    readFile(new URL("../../src/lib/launchGovernance.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/launch-gates/renderLaunchGates.js", import.meta.url),
      "utf8"),
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL(
      "../../../control-plane-api/src/metahub_control/launch_governance/routes.py",
      import.meta.url,
    ), "utf8"),
    readFile(new URL(
      "../../../control-plane-api/src/metahub_control/launch_governance/models.py",
      import.meta.url,
    ), "utf8"),
    readFile(new URL(
      "../../../infra/migrations/0130_saas_release_candidate_launch_gates.sql",
      import.meta.url,
    ), "utf8"),
  ]);
  assert.match(client, /credentials: "include"/u);
  assert.match(client, /cache: "no-store"/u);
  assert.doesNotMatch(client,
    /localStorage|sessionStorage|indexedDB|headers\.Authorization|["']Authorization["']|Bearer/u);
  assert.match(state, /launchIntent[\s\S]*idempotencyKey/u);
  assert.match(state, /signature = ""/u);
  assert.match(state, /verificationCode = ""[\s\S]*stepUpGrant = ""/u);
  assert.doesNotMatch(renderer, /tenant_id|artifact_uri|object_url|raw_log|endpoint_url/iu);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*platform-launch-gate-table/u);
  const routePaths = [...routes.matchAll(
    /@router\.(?:get|post)\(\s*"(\/platform\/[^"?]+)"/gu,
  )].map((match) => match[1]);
  assert.deepEqual(routePaths, [
    "/platform/launch-gates",
    "/platform/launch-gates/{launch_gate_id}/evidence",
    "/platform/launches",
    "/platform/launches/{launch_candidate_id}/rollback",
  ]);
  assert.doesNotMatch(`${client}\n${state}\n${renderer}\n${routes}\n${models}\n${migration}`,
    /general_availability/u);
  assert.doesNotMatch(`${routes}\n${renderer}`, /override|artifact_uri|tenant_id/iu);
});

function artifactInputs() {
  return LAUNCH_GATE_CODES.map((gateCode, index) => ({
    gate_code: gateCode,
    workspace_id: WORKSPACE_ID,
    artifact_sha256: hex(20 + index),
  }));
}

function waveInputs() {
  return LAUNCH_WAVES.map((waveKind, index) => ({
    wave_kind: waveKind,
    workspace_id: WORKSPACE_ID,
    cohort_manifest_sha256: hex(50 + index),
    feature_flag_generation: 1,
    migration_plan_sha256: hex(60 + index),
    rollback_plan_sha256: hex(70 + index),
    observation_seconds: 60,
  }));
}

function gates() {
  return LAUNCH_GATE_CODES.map((gateCode, index) => ({
    launch_gate_id: `lgt_${(index + 1).toString(16).padStart(32, "0")}`,
    gate_code: gateCode,
    policy_version: 1,
    verifier_code: `${gateCode}-verifier`,
    verifier_version: 1,
    threshold_rule: "exact_pass",
    freshness_seconds: gateCode === "long_stability" ? 14400 : 86400,
    blocking_severity: "p0",
    policy_sha256: hex(index + 1),
  }));
}

function candidate(extra = {}) {
  return {
    launch_candidate_id: CANDIDATE_ID,
    candidate_sha256: hex(80),
    code_tree_sha256: hex(100),
    migration_set_sha256: hex(101),
    contract_set_sha256: hex(102),
    artifact_set_sha256: hex(103),
    test_manifest_sha256: hex(104),
    gate_policy_set_sha256: hex(105),
    generation: 1,
    status: "collecting",
    revision: 1,
    created_at: NOW,
    updated_at: NOW,
    completed_at: null,
    evidence: [],
    decisions: [],
    waves: waveList(),
    rollbacks: [],
    review: null,
    ...extra,
  };
}

function evidence(extra = {}) {
  const gate = gates()[0];
  return {
    launch_evidence_id: `lve_${"3".repeat(32)}`,
    launch_gate_id: gate.launch_gate_id,
    gate_code: gate.gate_code,
    workspace_id: WORKSPACE_ID,
    artifact_sha256: hex(20),
    verifier_code: gate.verifier_code,
    verifier_version: 1,
    envelope_sha256: hex(120),
    outcome: "passed",
    metric_value: 1,
    metric_unit: "pass_ratio",
    environment_sha256: hex(121),
    provenance_sha256: hex(122),
    started_at: NOW,
    finished_at: NOW,
    expires_at: LATER,
    created_at: NOW,
    ...extra,
  };
}

function decision(extra = {}) {
  return {
    go_no_go_decision_id: `lgd_${"4".repeat(32)}`,
    actor_kind: "product",
    operator_id: `pto_${"5".repeat(32)}`,
    operator_revision: 1,
    decision: "approved",
    reason_code: "approved_release",
    decision_sha256: hex(130),
    signature_sha256: hex(131),
    decided_at: NOW,
    ...extra,
  };
}

function waveList(overrides = {}) {
  return LAUNCH_WAVES.map((waveKind, index) => ({
    launch_wave_id: `lvw_${(index + 1).toString(16).padStart(32, "0")}`,
    wave_kind: waveKind,
    wave_sequence: index + 1,
    workspace_id: WORKSPACE_ID,
    cohort_manifest_sha256: hex(140 + index),
    feature_flag_generation: 1,
    migration_plan_sha256: hex(150 + index),
    rollback_plan_sha256: hex(160 + index),
    observation_seconds: 60,
    status: "pending",
    observation_deadline_at: null,
    health_watermark_sha256: null,
    revision: 1,
    started_at: null,
    completed_at: null,
    ...(overrides[waveKind] ?? {}),
  }));
}

function rollback(extra = {}) {
  return {
    launch_rollback_id: `lrb_${"6".repeat(32)}`,
    workspace_id: WORKSPACE_ID,
    from_wave_kind: "internal",
    to_wave_kind: "pre_launch",
    trigger_code: "operator_request",
    trigger_sha256: hex(170),
    checkpoint_sha256: hex(171),
    job_receipt_sha256: hex(172),
    verification_sha256: null,
    status: "pending",
    revision: 1,
    requested_at: NOW,
    verified_at: null,
    ...extra,
  };
}

function review(extra = {}) {
  return {
    post_launch_review_id: `lpr_${"7".repeat(32)}`,
    workspace_id: WORKSPACE_ID,
    summary_sha256: hex(180),
    action_items_sha256: hex(181),
    conclusion: "release_candidate",
    reviewed_at: NOW,
    ...extra,
  };
}

function gateListResponse(value = candidate()) {
  return { gates: gates(), candidate: value, request_id: "req_prd138", trace_id: "trc_prd138" };
}

function launchResponse(value) {
  return { candidate: value, idempotent_replay: false,
    request_id: "req_prd138_launch", trace_id: "trc_prd138_launch" };
}

function evidenceResponse(proof) {
  return { evidence: proof, candidate: candidate({ evidence: [proof], revision: 2 }),
    idempotent_replay: false, request_id: "req_prd138_evidence",
    trace_id: "trc_prd138_evidence" };
}

function rollbackResponse(record, status) {
  return { rollback: record, candidate: candidate({ status, rollbacks: [record], revision: 6 }),
    idempotent_replay: false, request_id: "req_prd138_rollback",
    trace_id: "trc_prd138_rollback" };
}

function platformState(value) {
  return { loading: false, launchIntent: null, launchGovernance: gateListResponse(value) };
}

function shellState(value, permissions) {
  return {
    ...platformState(value),
    session: { context: { permissions: [...permissions],
      operator: { display_name: "Release", roles: ["platform_owner"] } } },
    navigation: [{ id: "launch-gates", label: "Launch gates",
      href: "/platform.html#launch-gates" }],
    view: "launch-gates",
    error: null,
    feedback: null,
    pendingAction: null,
  };
}

function hex(value) { return Number(value).toString(16).padStart(64, "0"); }
function responseInvalid(error) { return error instanceof PlatformApiError
  && error.code === "launch.response_invalid" && error.status === 502; }

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: {
    "Cache-Control": "private, no-store", "Content-Type": "application/json",
  } });
}

function installDocument(t) {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${CSRF}` };
  t.after(() => { globalThis.document = originalDocument; });
}

function installFetch(t, handler) {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return handler(String(url), options);
  };
  t.after(() => { globalThis.fetch = originalFetch; });
  return calls;
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}
