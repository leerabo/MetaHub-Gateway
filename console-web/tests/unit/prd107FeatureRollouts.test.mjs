import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import {
  commandFeatureRollout,
  createFeatureFlag,
  killFeatureFlag,
  listFeatureFlags,
} from "../../src/lib/featureRollouts.js";

const CONTROL_URL = "http://control.test";
const FLAG_ID = `ffg_${"1".repeat(32)}`;
const ROLLOUT_ID = `fro_${"2".repeat(32)}`;
const NOW = "2026-08-23T08:00:00Z";

test("feature rollout client rejects response extensions and keeps version history", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => jsonResponse(listResponse());
    const listed = await listFeatureFlags({ controlApiUrl: CONTROL_URL });
    assert.deepEqual(listed.flags[0].versions.map((item) => item.version_number), [2, 1]);

    globalThis.fetch = async () => jsonResponse({ ...listResponse(), bucket_salt: "hidden" });
    await assert.rejects(
      listFeatureFlags({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "feature_flag.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("version rollout and kill bind Cookie CSRF CAS and idempotency without bearer", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"p".repeat(43)}` };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return String(url).endsWith("/rollouts")
      ? jsonResponse(rolloutResponse())
      : jsonResponse(flagResponse());
  };
  try {
    await createFeatureFlag({
      controlApiUrl: CONTROL_URL,
      flag: versionPayload(),
      revision: 3,
      idempotencyKey: "prd107-console-version-0001",
    });
    await commandFeatureRollout({
      controlApiUrl: CONTROL_URL,
      flagId: FLAG_ID,
      action: "start",
      versionNumber: 2,
      percentageBps: 2500,
      environmentCode: "production",
      regionCode: "cn-north-1",
      revision: 3,
      idempotencyKey: "prd107-console-rollout-0001",
    });
    await killFeatureFlag({
      controlApiUrl: CONTROL_URL,
      flagId: FLAG_ID,
      revision: 3,
      reasonCode: "incident_containment",
      idempotencyKey: "prd107-console-kill-000001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
  assert.equal(calls.length, 3);
  assert.equal(calls.every((call) => call.options.credentials === "include"), true);
  assert.equal(calls.every((call) => call.options.cache === "no-store"), true);
  assert.equal(calls.every((call) => !("Authorization" in call.options.headers)), true);
  assert.equal(calls.every((call) => call.options.headers["If-Match"] === '"3"'), true);
  assert.equal(calls[0].options.headers["Idempotency-Key"], "prd107-console-version-0001");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "p".repeat(43));
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    action: "start",
    rollout_id: null,
    version_number: 2,
    percentage_bps: 2500,
    environment_code: "production",
    region_code: "cn-north-1",
    reason_code: "release_start",
    confirmed: true,
  });
});

test("feature rollout renderer independently prunes write approve and kill controls", () => {
  const writable = { innerHTML: "" };
  renderPlatformWorkspace(writable, platformState([
    "platform.feature.read", "platform.feature.write", "platform.feature.approve",
    "platform.feature.kill",
  ]));
  assert.match(writable.innerHTML, /功能开关与渐进发布/u);
  assert.match(writable.innerHTML, /data-platform-feature-create-form/u);
  assert.match(writable.innerHTML, /name="action" value="approve"/u);
  assert.match(writable.innerHTML, /data-platform-feature-kill-form/u);
  assert.match(writable.innerHTML, /不可变事实/u);
  assert.doesNotMatch(writable.innerHTML, /bucket_salt|rules_sha256|subject_pseudonym/u);

  const approver = { innerHTML: "" };
  renderPlatformWorkspace(approver, platformState([
    "platform.feature.read", "platform.feature.approve",
  ]));
  assert.match(approver.innerHTML, /name="action" value="approve"/u);
  assert.doesNotMatch(
    approver.innerHTML,
    /data-platform-feature-create-form|data-platform-feature-kill-form/u,
  );

  const readonly = { innerHTML: "" };
  renderPlatformWorkspace(readonly, platformState(["platform.feature.read"]));
  assert.doesNotMatch(
    readonly.innerHTML,
    /data-platform-feature-create-form|data-platform-feature-command-form|data-platform-feature-kill-form/u,
  );
});

test("feature rollout table collapses to labeled rows at 390px without local overrides", async () => {
  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  const client = await readFile(
    new URL("../../src/lib/featureRollouts.js", import.meta.url),
    "utf8",
  );
  assert.match(css, /\.platform-feature-table \{ min-width: 0;/u);
  assert.match(css, /\.platform-feature-table thead \{ display: none;/u);
  assert.match(css, /\.platform-feature-table td::before \{ content: attr\(data-label\)/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|client_override/u);
});

test("controller keeps approve independent and cancels late feature refresh on leave", async () => {
  const root = fakeRoot();
  const commands = [];
  let forbiddenCommands = 0;
  let featureCall = 0;
  let resolveLate;
  const late = new Promise((resolve) => { resolveLate = resolve; });
  const controller = createPlatformPageController({
    root,
    newIdempotencyKey: () => "prd107-controller-command-0001",
    api: {
      session: async () => ({
        context: {
          permissions: ["platform.feature.read", "platform.feature.approve"],
          operator: { display_name: "Approver", roles: ["release_manager"] },
        },
      }),
      navigation: async () => ({ items: [{
        id: "feature-rollouts",
        label: "Feature rollouts",
        href: "/platform.html#feature-rollouts",
      }] }),
      featureFlags: async () => {
        featureCall += 1;
        return featureCall === 3 ? late : listResponse();
      },
      commandFeatureRollout: async (input) => {
        commands.push(input);
        return rolloutResponse();
      },
      createFeatureFlag: async () => { forbiddenCommands += 1; },
      killFeatureFlag: async () => { forbiddenCommands += 1; },
    },
  });
  assert.equal(await controller.mount(), true);
  controller.selectView("feature-rollouts");
  assert.equal(await controller.createFeatureFlag({ flag: versionPayload(), revision: 3 }), false);
  assert.equal(await controller.killFeatureFlag({ confirmed: true }), false);
  assert.equal(forbiddenCommands, 0);
  assert.equal(await controller.commandFeatureRollout({
    flagId: FLAG_ID,
    rolloutId: ROLLOUT_ID,
    action: "approve",
    revision: 1,
    confirmed: true,
  }), true);
  assert.equal(commands.length, 1);
  assert.equal(commands[0].idempotencyKey, "prd107-controller-command-0001");
  assert.equal(commands[0].versionNumber, null);

  const refresh = controller.filterFeatureFlags("active", "p0");
  controller.selectView("overview");
  resolveLate(listResponse());
  assert.equal(await refresh, false);
  assert.match(root.innerHTML, /平台工作台/u);
});

function platformState(permissions) {
  return {
    session: {
      context: {
        permissions,
        operator: { display_name: "Release", roles: ["release_manager"] },
      },
    },
    navigation: [{
      id: "feature-rollouts",
      label: "Feature rollouts",
      href: "/platform.html#feature-rollouts",
    }],
    view: "feature-rollouts",
    featureFlags: listResponse(),
    featureStatus: "",
    featureRiskLevel: "",
    loading: false,
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

function versionPayload() {
  return {
    action: "version",
    flag_id: FLAG_ID,
    flag_code: "gateway.new_router",
    display_name: "New router",
    risk_level: "p0",
    rules: rules(10000),
    confirmed: true,
  };
}

function listResponse() {
  return {
    flags: [flag()],
    total: 1,
    request_id: "request-features",
    trace_id: "trace-features",
  };
}

function flag() {
  return {
    flag_id: FLAG_ID,
    flag_code: "gateway.new_router",
    display_name: "New router",
    risk_level: "p0",
    status: "active",
    current_version: 2,
    rollout_percentage_bps: 2500,
    revision: 3,
    kill_generation: 0,
    kill_reason_code: null,
    exposure_count: 12,
    rules: rules(10000),
    versions: [version(2, 8), version(1, 4)],
    latest_rollout: rollout(),
    created_at: NOW,
    updated_at: NOW,
  };
}

function rules(percentageBps) {
  return {
    environments: ["production"],
    regions: ["cn-north-1"],
    cohort_code: null,
    percentage_bps: percentageBps,
    prerequisite_flag_code: null,
  };
}

function version(versionNumber, exposureCount) {
  return {
    version_number: versionNumber,
    rules: rules(10000),
    exposure_count: exposureCount,
    created_at: NOW,
  };
}

function rollout() {
  return {
    rollout_id: ROLLOUT_ID,
    action: "start",
    status: "pending_approval",
    version_number: 2,
    target_percentage_bps: 2500,
    environment_code: "production",
    region_code: "cn-north-1",
    health_status: "pending",
    health_source_watermark: null,
    approval_count: 1,
    revision: 1,
    job_id: null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function flagResponse() {
  return {
    flag: flag(),
    idempotent_replay: false,
    request_id: "request-feature",
    trace_id: "trace-feature",
  };
}

function rolloutResponse() {
  return {
    flag: flag(),
    rollout: rollout(),
    idempotent_replay: false,
    request_id: "request-rollout",
    trace_id: "trace-rollout",
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(payload); },
  };
}
