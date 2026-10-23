import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import {
  applySecurityRemediation,
  getSecurityAssessment,
  startSecurityAssessment,
} from "../../src/lib/securityAssurance.js";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const ASSESSMENT_ID = `sas_${"2".repeat(32)}`;
const OTHER_ASSESSMENT_ID = `sas_${"3".repeat(32)}`;
const RUN_ID = `itr_${"4".repeat(32)}`;
const FINDING_ID = `ptf_${"5".repeat(32)}`;
const REMEDIATION_ID = `rem_${"6".repeat(32)}`;
const RISK_ID = `rsk_${"7".repeat(32)}`;
const CSRF = "C".repeat(43);
const STEP_UP = "G".repeat(43);
const IDEMPOTENCY = "security-assurance-command-0001";
const CODE_TREE_SHA = "a".repeat(64);
const MIGRATION_SHA = "b".repeat(64);
const CONTRACT_SHA = "c".repeat(64);
const ARTIFACT_SHA = "d".repeat(64);
const CHANGE_SHA = "e".repeat(64);
const NOW = "2026-08-31T08:00:00.000Z";
const LATER = "2026-09-01T08:00:00.000Z";

test("PRD-137 clients keep the three fixed Cookie routes and exact write headers", async (t) => {
  installDocument(t);
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/platform/security/assessments" && options.method === "POST") {
      return jsonResponse(assessmentResponse(), 201);
    }
    if (path === `/platform/security/assessments/${ASSESSMENT_ID}`) {
      return jsonResponse(assessmentResponse());
    }
    if (path === `/platform/security/findings/${FINDING_ID}/remediations`) {
      return jsonResponse(remediationResponse(), 201);
    }
    throw new Error(`unexpected route ${path}`);
  });

  const started = await startSecurityAssessment(assessmentCommand());
  const loaded = await getSecurityAssessment({
    controlApiUrl: CONTROL_URL,
    assessmentId: ASSESSMENT_ID,
  });
  const remediated = await applySecurityRemediation({
    controlApiUrl: CONTROL_URL,
    findingId: FINDING_ID,
    operation: "propose",
    expectedRevision: 2,
    changeSha256: CHANGE_SHA,
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });

  assert.equal(started.assessment.workspace_id, WORKSPACE_ID);
  assert.equal(loaded.assessment.security_assessment_id, ASSESSMENT_ID);
  assert.equal(remediated.remediation.remediation_id, REMEDIATION_ID);
  assert.deepEqual(calls.map(({ url }) => new URL(url).pathname), [
    "/platform/security/assessments",
    `/platform/security/assessments/${ASSESSMENT_ID}`,
    `/platform/security/findings/${FINDING_ID}/remediations`,
  ]);
  calls.forEach(({ options }) => {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
  });
  assert.equal(calls[0].options.headers["X-CSRF-Token"], CSRF);
  assert.equal(calls[0].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(calls[0].options.headers["Idempotency-Key"], IDEMPOTENCY);
  assert.equal(calls[0].options.headers["If-Match"], '"1"');
  assert.equal(calls[1].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[2].options.headers["If-Match"], '"2"');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    workspace_id: WORKSPACE_ID,
    profile_code: "incremental_release",
    profile_version: 1,
    code_tree_sha256: CODE_TREE_SHA,
    migration_set_sha256: MIGRATION_SHA,
    contract_set_sha256: CONTRACT_SHA,
    artifact_sha256: ARTIFACT_SHA,
    confirmed: true,
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    operation: "propose",
    expected_revision: 2,
    change_sha256: CHANGE_SHA,
    remediation_id: null,
    verification_assessment_id: null,
    confirmed: true,
  });
});

test("PRD-137 clients reject response extensions and assessment or finding owner drift", async (t) => {
  installDocument(t);
  installFetch(t, () => jsonResponse(assessmentResponse({ endpoint_url: "http://internal" })));
  await assert.rejects(
    getSecurityAssessment({ controlApiUrl: CONTROL_URL, assessmentId: ASSESSMENT_ID }),
    responseInvalid,
  );

  globalThis.fetch = async () => jsonResponse(assessmentResponse({
    security_assessment_id: OTHER_ASSESSMENT_ID,
  }));
  await assert.rejects(
    getSecurityAssessment({ controlApiUrl: CONTROL_URL, assessmentId: ASSESSMENT_ID }),
    responseInvalid,
  );

  globalThis.fetch = async () => jsonResponse(assessmentResponse({
    findings: [finding({ isolation_test_run_id: `itr_${"9".repeat(32)}` })],
  }));
  await assert.rejects(
    getSecurityAssessment({ controlApiUrl: CONTROL_URL, assessmentId: ASSESSMENT_ID }),
    responseInvalid,
  );
});

test("PRD-137 renderer exposes cropped proofs and workflows without exploit material", () => {
  const root = fakeRoot();
  renderPlatformWorkspace(root, platformState(assessment()));
  assert.match(root.innerHTML, /安全复核/u);
  assert.match(root.innerHTML, /增量发布/u);
  assert.match(root.innerHTML, /场景运行/u);
  assert.match(root.innerHTML, /Cleanup receipts[\s\S]*1[\s\S]*1 scenario runs/u);
  assert.match(root.innerHTML, /问题与改进/u);
  assert.match(root.innerHTML, /data-platform-assurance-remediation-form/u);
  assert.match(root.innerHTML, /Risk acceptance/u);
  assert.doesNotMatch(root.innerHTML, new RegExp(CODE_TREE_SHA, "u"));
  assert.doesNotMatch(root.innerHTML,
    /endpoint_url|source_subject|target_subject|seed_receipt|exploit|payload|Authorization|Bearer/iu);
});

test("PRD-137 controller consumes step-up, reuses retry idempotency, and honors revocation", async (t) => {
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash: "#security-assurance" },
    history: { replaceState() {} },
  };
  t.after(() => { globalThis.window = originalWindow; });
  let permissions = [
    "platform.assurance.read", "platform.assurance.run", "platform.assurance.remediate",
  ];
  let attempts = 0;
  const starts = [];
  const grants = [];
  const api = {
    session: async () => ({ context: {
      permissions: [...permissions],
      operator: { display_name: "Security", roles: ["platform_security"] },
    } }),
    navigation: async () => ({
      items: [{
        id: "security-assurance",
        label: "Security assurance",
        href: "/platform.html#security-assurance",
      }],
    }),
    createIdentityStepUpGrant: async (input) => {
      grants.push(input);
      return { grant: STEP_UP };
    },
    startSecurityAssessment: async (input) => {
      starts.push(input);
      attempts += 1;
      if (attempts === 1) {
        throw new PlatformApiError({
          status: 503,
          code: "security_assurance.unavailable",
          message: "retry",
        });
      }
      return assessmentResponse();
    },
    securityAssessment: async () => assessmentResponse(),
  };
  const controller = createPlatformPageController({
    root: fakeRoot(),
    api,
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  assert.equal(await controller.mount(), true);
  const command = {
    workspaceId: WORKSPACE_ID,
    profileCode: "incremental_release",
    codeTreeSha256: CODE_TREE_SHA,
    migrationSetSha256: MIGRATION_SHA,
    contractSetSha256: CONTRACT_SHA,
    artifactSha256: ARTIFACT_SHA,
    method: "totp",
    verificationCode: "123456",
    confirmed: true,
  };
  assert.equal(await controller.startSecurityAssessment(command), false);
  assert.equal(await controller.startSecurityAssessment(command), true);
  assert.deepEqual(starts.map((item) => item.idempotencyKey), [IDEMPOTENCY, IDEMPOTENCY]);
  assert.deepEqual(grants.map((item) => ({
    actionCode: item.actionCode,
    resourceType: item.resourceType,
    resourceId: item.resourceId,
  })), [
    {
      actionCode: "platform.assurance.run",
      resourceType: "security_assurance_profile",
      resourceId: "incremental_release",
    },
    {
      actionCode: "platform.assurance.run",
      resourceType: "security_assurance_profile",
      resourceId: "incremental_release",
    },
  ]);
  assert.equal(starts.every((item) => item.verificationCode === undefined), true);

  permissions = ["platform.assurance.read"];
  assert.equal(await controller.refresh(), true);
  assert.equal(await controller.startSecurityAssessment(command), false);
  assert.equal(starts.length, 2);
  controller.destroy();
});

test("PRD-137 sources retain fixed routes, responsive layout, and no secret persistence", async () => {
  const [client, state, renderer, css, routes] = await Promise.all([
    readFile(new URL("../../src/lib/securityAssurance.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url),
      "utf8"),
    readFile(new URL("../../src/features/platform/renderPlatformPage.js", import.meta.url),
      "utf8"),
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL(
      "../../../control-plane-api/src/metahub_control/security_assurance/routes.py",
      import.meta.url,
    ), "utf8"),
  ]);
  assert.match(client, /credentials: "include"/u);
  assert.match(client, /cache: "no-store"/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|indexedDB|headers\.Authorization|["']Authorization["']|Bearer/u);
  assert.match(state, /securityAssuranceIntent[\s\S]*idempotencyKey/u);
  assert.match(state, /verificationCode = ""[\s\S]*stepUpGrant = ""/u);
  assert.doesNotMatch(renderer, /endpoint_url|exploit_payload|source_subject_sha256/u);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*platform-assurance-runs/u);
  const routePaths = [...routes.matchAll(
    /@router\.(?:get|post)\(\s*"(\/platform\/security\/[^"?]+)"/gu,
  )].map((match) => match[1]);
  assert.deepEqual(routePaths, [
    "/platform/security/assessments",
    "/platform/security/assessments/{assessment_id}",
    "/platform/security/findings/{finding_id}/remediations",
  ]);
  assert.doesNotMatch(routes, /script_url|endpoint_url|sql_text|production_selector/u);
});

function assessmentCommand() {
  return {
    controlApiUrl: CONTROL_URL,
    workspaceId: WORKSPACE_ID,
    profileCode: "incremental_release",
    profileVersion: 1,
    codeTreeSha256: CODE_TREE_SHA,
    migrationSetSha256: MIGRATION_SHA,
    contractSetSha256: CONTRACT_SHA,
    artifactSha256: ARTIFACT_SHA,
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  };
}

function assessmentResponse(extra = {}) {
  return {
    assessment: assessment(extra),
    idempotent_replay: false,
    request_id: "req_prd137",
    trace_id: "trc_prd137",
  };
}

function assessment(extra = {}) {
  return {
    security_assessment_id: ASSESSMENT_ID,
    workspace_id: WORKSPACE_ID,
    profile_code: "incremental_release",
    profile_version: 1,
    profile_sha256: "0".repeat(64),
    profile_signature_sha256: "1".repeat(64),
    code_tree_sha256: CODE_TREE_SHA,
    migration_set_sha256: MIGRATION_SHA,
    contract_set_sha256: CONTRACT_SHA,
    artifact_sha256: ARTIFACT_SHA,
    scope_sha256: "2".repeat(64),
    status: "failed",
    generation: 1,
    environment_receipt_sha256: "3".repeat(64),
    summary_sha256: "4".repeat(64),
    revision: 2,
    requested_at: NOW,
    started_at: NOW,
    finished_at: NOW,
    runs: [run()],
    findings: [finding()],
    ...extra,
  };
}

function run(extra = {}) {
  return {
    isolation_test_run_id: RUN_ID,
    scenario_code: "api_idor",
    target_kind: "public_contract",
    owner_kind: "workspace_resource",
    contract_code: "workspace.resource.read",
    weakness_code: "idor.cross_workspace",
    owner_registry_version: 1,
    runner_image_sha256: "5".repeat(64),
    outcome: "failed",
    duration_ms: 812,
    evidence_sha256: "6".repeat(64),
    cleanup_receipt_sha256: "7".repeat(64),
    started_at: NOW,
    finished_at: NOW,
    ...extra,
  };
}

function finding(extra = {}) {
  return {
    pen_test_finding_id: FINDING_ID,
    isolation_test_run_id: RUN_ID,
    owner_kind: "workspace_resource",
    contract_code: "workspace.resource.read",
    severity: "p0",
    weakness_code: "idor.cross_workspace",
    status: "open",
    sla_deadline_at: LATER,
    evidence_sha256: "6".repeat(64),
    revision: 2,
    first_seen_at: NOW,
    last_seen_at: NOW,
    closed_at: null,
    remediations: [],
    risk_acceptances: [riskAcceptance()],
    ...extra,
  };
}

function remediation() {
  return {
    remediation_id: REMEDIATION_ID,
    change_sha256: CHANGE_SHA,
    verification_assessment_id: null,
    outcome: "proposed",
    finding_revision: 2,
    created_at: NOW,
    verified_at: null,
  };
}

function remediationResponse() {
  return {
    remediation: remediation(),
    finding: finding({ revision: 3, status: "remediation_pending", remediations: [remediation()] }),
    idempotent_replay: false,
    request_id: "req_prd137_remediation",
    trace_id: "trc_prd137_remediation",
  };
}

function riskAcceptance() {
  return {
    risk_acceptance_id: RISK_ID,
    scope_sha256: "8".repeat(64),
    compensating_controls: ["network_isolation"],
    status: "pending_owner",
    revision: 1,
    expires_at: LATER,
    approved_at: null,
    updated_at: NOW,
  };
}

function platformState(value) {
  return {
    loading: false,
    session: { context: {
      permissions: [
        "platform.assurance.read", "platform.assurance.run", "platform.assurance.remediate",
      ],
      operator: { display_name: "Security", roles: ["platform_security"] },
    } },
    navigation: [{
      id: "security-assurance",
      label: "Security assurance",
      href: "/platform.html#security-assurance",
    }],
    view: "security-assurance",
    securityAssurance: value,
    securityAssuranceLookup: ASSESSMENT_ID,
    securityAssuranceIntent: null,
    error: null,
    feedback: null,
    pendingAction: null,
  };
}

function responseInvalid(error) {
  return error instanceof PlatformApiError
    && error.code === "security_assurance.response_invalid" && error.status === 502;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Cache-Control": "private, no-store",
      "Content-Type": "application/json",
    },
  });
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
