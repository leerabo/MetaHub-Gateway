import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  completeRiskChallenge,
  createRiskAppeal,
  decideRiskCase,
  listRiskCases,
} from "../../src/lib/riskControls.js";

const CONTROL_URL = "http://control.test";
const NOW = "2026-08-24T08:00:00Z";
const CASE_ID = `rca_${"1".repeat(32)}`;
const CHALLENGE_ID = `rch_${"2".repeat(32)}`;
const RESTRICTION_ID = `rst_${"3".repeat(32)}`;
const APPEAL_ID = `rap_${"4".repeat(32)}`;

test("risk clients enforce strict DTOs and Cookie CSRF writes", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = {
    cookie: [
      `__Host-metahub-platform-csrf=${"p".repeat(43)}`,
      `__Host-metahub-global-csrf=${"g".repeat(43)}`,
      `__Host-metahub-workspace-csrf=${"w".repeat(43)}`,
    ].join("; "),
  };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    const payload = calls.length === 1 ? caseList()
      : calls.length === 2 ? decisionResponse()
        : calls.length === 3 ? challengeResponse()
          : appealResponse();
    return jsonResponse(payload, calls.length === 1 ? 200 : 201);
  };
  try {
    assert.equal((await listRiskCases({ controlApiUrl: CONTROL_URL })).projection, "security");
    await decideRiskCase({
      controlApiUrl: CONTROL_URL,
      caseId: CASE_ID,
      revision: 1,
      decision: "restricted",
      reasonCode: "automation_confirmed",
      restrictionKind: "api_calls",
      restrictionMinutes: 30,
      idempotencyKey: "prd110-risk-decision-0001",
    });
    await completeRiskChallenge({
      controlApiUrl: CONTROL_URL,
      challengeId: CHALLENGE_ID,
      challengeToken: "t".repeat(43),
      answer: "123456",
      revision: 1,
      credential: "global",
      idempotencyKey: "prd110-risk-challenge-0001",
    });
    await createRiskAppeal({
      controlApiUrl: CONTROL_URL,
      restrictionId: RESTRICTION_ID,
      category: "not_my_activity",
      idempotencyKey: "prd110-risk-appeal-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }

  assert.equal(calls.every((call) => call.options.credentials === "include"), true);
  assert.equal(calls.every((call) => call.options.cache === "no-store"), true);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "p".repeat(43));
  assert.equal(calls[1].options.headers["If-Match"], '"1"');
  assert.equal(calls[2].options.headers["X-CSRF-Token"], "g".repeat(43));
  assert.equal(calls[3].options.headers["X-CSRF-Token"], "w".repeat(43));
  assert.equal(calls.some((call) => "Authorization" in call.options.headers), false);
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    challenge_id: CHALLENGE_ID,
    challenge_token: "t".repeat(43),
    answer: "123456",
    revision: 1,
    confirmed: true,
  });

  const original = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse({ ...caseList(), tenant_id: "secret" });
  try {
    await assert.rejects(
      listRiskCases({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "risk.response_invalid",
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("platform risk renderer independently redacts support and removes controls", () => {
  const security = fakeRoot();
  renderPlatformWorkspace(security, platformState([
    "platform.risk.read",
    "platform.risk.decide",
  ], caseList()));
  assert.match(security.innerHTML, /data-platform-risk-decision-form/u);
  assert.match(security.innerHTML, /request velocity/u);
  assert.match(security.innerHTML, /high/u);
  assert.match(security.innerHTML, /申诉时间线/u);

  const supportProjection = caseList();
  supportProjection.projection = "support";
  supportProjection.items = supportProjection.items.map((item) => ({
    ...item,
    score_band: null,
    signal_families: [],
  }));
  const support = fakeRoot();
  renderPlatformWorkspace(support, platformState(
    ["platform.risk.support_read"],
    supportProjection,
  ));
  assert.doesNotMatch(support.innerHTML, /data-platform-risk-decision-form/u);
  assert.doesNotMatch(support.innerHTML, /request velocity|high/u);
  assert.match(support.innerHTML, /支持投影不显示/u);
});

test("platform risk controller fences writes and refreshes the active projection", async () => {
  const calls = [];
  const controller = createPlatformPageController({
    root: fakeRoot(),
    newIdempotencyKey: () => "prd110-controller-decision-0001",
    api: platformApi(calls, ["platform.risk.read", "platform.risk.decide"]),
  });
  assert.equal(await controller.mount(), true);
  controller.selectView("abuse-risk");
  assert.equal(await controller.decideRiskCase({
    caseId: CASE_ID,
    revision: 1,
    decision: "required_review",
    reasonCode: "manual_review",
    confirmed: true,
  }), true);
  assert.deepEqual(calls, ["list", "write", "list"]);

  const deniedCalls = [];
  const denied = createPlatformPageController({
    root: fakeRoot(),
    api: platformApi(deniedCalls, ["platform.risk.support_read"]),
  });
  assert.equal(await denied.mount(), true);
  assert.equal(await denied.decideRiskCase({
    caseId: CASE_ID,
    revision: 1,
    decision: "required_review",
    reasonCode: "manual_review",
    confirmed: true,
  }), false);
  assert.equal(deniedCalls.includes("write"), false);
});

test("identity challenge keeps token in memory and clears answers on success and failure", async () => {
  const answerInput = { value: "123456" };
  const successRoot = identityRoot(answerInput);
  const received = [];
  const success = createIdentityPageController({
    root: successRoot,
    newIdempotencyKey: () => "prd110-identity-challenge-0001",
    api: identityApi({
      completeRiskChallenge: async (input) => {
        received.push(input);
        return challengeResponse();
      },
    }),
  });
  assert.equal(success.showRiskChallenge({
    challengeId: CHALLENGE_ID,
    challengeToken: "t".repeat(43),
    revision: 1,
  }), true);
  assert.doesNotMatch(successRoot.innerHTML, new RegExp("t{20}", "u"));
  assert.equal(await success.completeRiskChallenge({ answer: "123456" }), true);
  assert.equal(answerInput.value, "");
  assert.equal(received[0].challengeToken, "t".repeat(43));

  const failureInput = { value: "654321" };
  const failureRoot = identityRoot(failureInput);
  const failure = createIdentityPageController({
    root: failureRoot,
    api: identityApi({
      completeRiskChallenge: async () => {
        throw Object.assign(new Error("invalid"), {
          code: "risk.challenge_invalid",
          status: 401,
          requestId: "request-risk-invalid",
        });
      },
    }),
  });
  failure.showRiskChallenge({
    challengeId: CHALLENGE_ID,
    challengeToken: "z".repeat(43),
    revision: 1,
  });
  assert.equal(await failure.completeRiskChallenge({ answer: "654321" }), false);
  assert.equal(failureInput.value, "");
  assert.doesNotMatch(failureRoot.innerHTML, new RegExp("z{20}|654321", "u"));
});

test("risk CSS collapses cases to labeled rows and clients never use browser storage", async () => {
  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  const client = await readFile(new URL("../../src/lib/riskControls.js", import.meta.url), "utf8");
  assert.match(css, /\.platform-risk-table \{ min-width: 0;/u);
  assert.match(css, /\.platform-risk-table thead \{ display: none;/u);
  assert.match(css, /\.platform-risk-table td::before \{ content: attr\(data-label\)/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|indexedDB/iu);
});

function platformApi(calls, permissions) {
  return {
    session: async () => ({ context: {
      permissions,
      operator: { display_name: "Operator", roles: ["platform_security"] },
    } }),
    navigation: async () => ({ items: [{
      id: "abuse-risk",
      label: "Abuse & Risk",
      href: "/platform.html#abuse-risk",
    }] }),
    riskCases: async () => { calls.push("list"); return caseList(); },
    decideRiskCase: async () => { calls.push("write"); return decisionResponse(); },
  };
}

function platformState(permissions, response) {
  return {
    session: { context: {
      permissions,
      operator: { display_name: "Operator", roles: ["platform_security"] },
    } },
    navigation: [{ id: "abuse-risk", label: "Abuse & Risk", href: "#abuse-risk" }],
    view: "abuse-risk",
    riskCases: response,
    riskStatus: "",
    riskCursorHistory: [],
    loading: false,
  };
}

function caseList() {
  return {
    items: [riskCase()],
    total: 1,
    limit: 50,
    next_cursor: null,
    projection: "security",
    request_id: "request-prd110-cases",
    trace_id: "trace-prd110-cases",
  };
}

function riskCase(overrides = {}) {
  return {
    risk_case_id: CASE_ID,
    workspace_id: `wsp_${"5".repeat(32)}`,
    case_kind: "api_abuse",
    safe_reason_family: "unusual_volume",
    score_band: "high",
    status: "open",
    signal_families: ["request_velocity"],
    restriction: null,
    appeals: [appeal()],
    revision: 1,
    opened_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function restriction() {
  return {
    risk_restriction_id: RESTRICTION_ID,
    restriction_kind: "api_calls",
    status: "active",
    starts_at: NOW,
    expires_at: "2026-08-24T08:30:00Z",
    cleared_at: null,
    revision: 1,
  };
}

function appeal() {
  return {
    risk_appeal_id: APPEAL_ID,
    category: "not_my_activity",
    status: "pending",
    resolution_reason: null,
    revision: 1,
    created_at: NOW,
    resolved_at: null,
  };
}

function decisionResponse() {
  return {
    case: riskCase({ status: "restricted", restriction: restriction(), revision: 2 }),
    decision: {
      risk_decision_id: `rdc_${"6".repeat(32)}`,
      risk_case_id: CASE_ID,
      decision: "restricted",
      safe_reason_family: "unusual_volume",
      policy_version: 1,
      expires_at: "2026-08-24T08:30:00Z",
      decided_at: NOW,
    },
    idempotent_replay: false,
    request_id: "request-prd110-decision",
    trace_id: "trace-prd110-decision",
  };
}

function challengeResponse() {
  return {
    challenge: {
      risk_challenge_id: CHALLENGE_ID,
      challenge_kind: "proof_of_control",
      status: "consumed",
      attempts: 1,
      max_attempts: 5,
      expires_at: "2026-08-24T08:10:00Z",
      revision: 2,
    },
    completed: true,
    idempotent_replay: false,
    request_id: "request-prd110-challenge",
    trace_id: "trace-prd110-challenge",
  };
}

function appealResponse() {
  return {
    appeal: appeal(),
    idempotent_replay: false,
    request_id: "request-prd110-appeal",
    trace_id: "trace-prd110-appeal",
  };
}

function identityApi(overrides = {}) {
  const current = {
    session_id: `gis_${"7".repeat(32)}`,
    device_family: "chrome",
    created_at: NOW,
    last_seen_at: NOW,
    expires_at: "2026-08-25T08:00:00Z",
    is_current: true,
  };
  return {
    me: async () => ({
      account: { email: "owner@example.invalid", display_name: "Owner", status: "active", mfa_configured: true },
      current_session: current,
      request_id: "request-me",
      trace_id: "trace-me",
    }),
    sessions: async () => ({ sessions: [current], total: 1, request_id: "request-sessions", trace_id: "trace-sessions" }),
    workspaces: async () => ({ workspaces: [], total: 0, limit: 20, offset: 0, request_id: "request-workspaces", trace_id: "trace-workspaces" }),
    ...overrides,
  };
}

function identityRoot(answerInput) {
  return {
    innerHTML: "",
    addEventListener() {},
    contains() { return true; },
    querySelectorAll(selector) {
      if (selector === 'input[type="password"]' || selector === 'input[name="answer"]'
        || selector === 'input[autocomplete="one-time-code"]') return [answerInput];
      return [];
    },
  };
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    replaceChildren() { this.innerHTML = ""; },
    querySelectorAll() { return []; },
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(payload); },
  };
}
