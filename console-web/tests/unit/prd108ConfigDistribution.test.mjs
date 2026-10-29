import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  listConfigDistribution,
  rebuildWorkspaceConfig,
} from "../../src/lib/configDistribution.js";

const CONTROL_URL = "http://control.test";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const NOW = "2026-08-23T08:00:00Z";

test("configuration distribution client rejects response extensions", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => jsonResponse(listResponse());
    const response = await listConfigDistribution({ controlApiUrl: CONTROL_URL });
    assert.equal(response.items[0].minimum_applied_generation, 3);

    globalThis.fetch = async () => jsonResponse({ ...listResponse(), manifest: {} });
    await assert.rejects(
      listConfigDistribution({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "config_distribution.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("configuration rebuild binds Cookie CSRF and idempotency without bearer", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"p".repeat(43)}` };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(rebuildResponse(), 201);
  };
  try {
    await rebuildWorkspaceConfig({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      reasonCode: "consistency_repair",
      idempotencyKey: "prd108-console-rebuild-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal("Authorization" in calls[0].options.headers, false);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "p".repeat(43));
  assert.equal(calls[0].options.headers["Idempotency-Key"], "prd108-console-rebuild-0001");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    workspace_id: WORKSPACE_ID,
    reason_code: "consistency_repair",
    confirmed: true,
  });
});

test("renderer independently prunes rebuild and never renders manifest material", () => {
  const writable = { innerHTML: "" };
  renderPlatformWorkspace(writable, platformState([
    "platform.config_distribution.read",
    "platform.config_distribution.rebuild",
  ]));
  assert.match(writable.innerHTML, /配置分发/u);
  assert.match(writable.innerHTML, /确认水位/u);
  assert.match(writable.innerHTML, /data-platform-config-rebuild-form/u);
  assert.match(writable.innerHTML, /结果 [0-9a-f]{12}/u);
  assert.doesNotMatch(writable.innerHTML, /manifest_sha256|provider_key_refs|secret|token/iu);

  const readonly = { innerHTML: "" };
  renderPlatformWorkspace(
    readonly,
    platformState(["platform.config_distribution.read"]),
  );
  assert.match(readonly.innerHTML, /只读/u);
  assert.doesNotMatch(readonly.innerHTML, /data-platform-config-rebuild-form/u);
});

test("controller enforces independent rebuild permission and refreshes watermarks", async () => {
  const root = fakeRoot();
  const rebuilds = [];
  let reads = 0;
  const controller = createPlatformPageController({
    root,
    newIdempotencyKey: () => "prd108-controller-rebuild-0001",
    api: {
      session: async () => ({ context: {
        permissions: [
          "platform.config_distribution.read",
          "platform.config_distribution.rebuild",
        ],
        operator: { display_name: "Operator", roles: ["platform_ops"] },
      } }),
      navigation: async () => ({ items: [{
        id: "config-distribution",
        label: "Config distribution",
        href: "/platform.html#config-distribution",
      }] }),
      configDistribution: async () => { reads += 1; return listResponse(); },
      rebuildWorkspaceConfig: async (input) => { rebuilds.push(input); return rebuildResponse(); },
    },
  });
  assert.equal(await controller.mount(), true);
  controller.selectView("config-distribution");
  assert.equal(await controller.rebuildWorkspaceConfig({
    workspaceId: WORKSPACE_ID,
    reasonCode: "operator_rebuild",
    confirmed: true,
  }), true);
  assert.equal(rebuilds.length, 1);
  assert.equal(rebuilds[0].idempotencyKey, "prd108-controller-rebuild-0001");
  assert.equal(reads, 2);

  const readonly = createPlatformPageController({
    root: fakeRoot(),
    api: {
      session: async () => ({ context: {
        permissions: ["platform.config_distribution.read"],
        operator: { display_name: "Security", roles: ["platform_security"] },
      } }),
      navigation: async () => ({ items: [{
        id: "config-distribution",
        label: "Config distribution",
        href: "/platform.html#config-distribution",
      }] }),
      configDistribution: async () => listResponse(),
      rebuildWorkspaceConfig: async () => { throw new Error("must not execute"); },
    },
  });
  assert.equal(await readonly.mount(), true);
  assert.equal(await readonly.rebuildWorkspaceConfig({
    workspaceId: WORKSPACE_ID,
    reasonCode: "operator_rebuild",
    confirmed: true,
  }), false);
});

test("configuration table collapses at mobile width without Storage overrides", async () => {
  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  const client = await readFile(
    new URL("../../src/lib/configDistribution.js", import.meta.url),
    "utf8",
  );
  assert.match(css, /\.platform-config-table \{ min-width: 0;/u);
  assert.match(css, /\.platform-config-table thead \{ display: none;/u);
  assert.match(css, /\.platform-config-table td::before \{ content: attr\(data-label\)/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|client_override/u);
});

function platformState(permissions) {
  return {
    session: { context: {
      permissions,
      operator: { display_name: "Operator", roles: ["platform_ops"] },
    } },
    navigation: [{
      id: "config-distribution",
      label: "Config distribution",
      href: "/platform.html#config-distribution",
    }],
    view: "config-distribution",
    configDistribution: listResponse(),
    loading: false,
  };
}

function listResponse() {
  return {
    items: [{
      workspace_id: WORKSPACE_ID,
      cell_code: "cn-north-1-a",
      generation: 4,
      revocation_watermark: 2,
      manifest_sha256: "a".repeat(64),
      gateway_count: 3,
      minimum_applied_generation: 3,
      minimum_lkg_generation: 3,
      stale_gateway_count: 1,
      latest_ack_at: NOW,
      latest_rebuild: rebuild(),
    }],
    total: 1,
    request_id: "request-prd108-list",
    trace_id: "trace-prd108-list",
  };
}

function rebuild() {
  return {
    rebuild_job_id: `crj_${"2".repeat(32)}`,
    workspace_id: WORKSPACE_ID,
    target_generation: 4,
    platform_job_id: `pjob_${"3".repeat(32)}`,
    status: "completed",
    result_manifest_sha256: "b".repeat(64),
    revision: 3,
    created_at: NOW,
    completed_at: NOW,
  };
}

function rebuildResponse() {
  return {
    rebuild: rebuild(),
    idempotent_replay: false,
    request_id: "request-prd108-rebuild",
    trace_id: "trace-prd108-rebuild",
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
