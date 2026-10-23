import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import { getPublicStatus, mutatePublicStatusSubscription } from "../../src/lib/publicStatus.js";
import {
  approvePlatformSecurityIncident,
  createPlatformIncident,
  getPlatformStatusIncidents,
} from "../../src/lib/statusIncidents.js";

const CONTROL_URL = "http://control.test";
const STATUS_URL = "http://status.test";
const NOW = "2026-08-25T08:00:00Z";
const INCIDENT_ID = `inc_${"1".repeat(32)}`;
const UPDATE_ID = `iup_${"2".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"3".repeat(32)}`;

test("public status timestamps render in Chinese without incompatible Intl options", async () => {
  const { formatTime, isPublicStatusStale } = await import("../../src/features/public-status/main.js");
  assert.match(formatTime(NOW), /2026/u);
  assert.equal(isPublicStatusStale(NOW, Date.parse("2026-08-25T08:15:00Z")), false);
  assert.equal(isPublicStatusStale(NOW, Date.parse("2026-08-25T08:15:01Z")), true);
});

test("public status client rejects extensions and omits credentials", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url, options) => {
      calls.push({ url: String(url), options });
      return jsonResponse(publicResponse());
    };
    assert.equal((await getPublicStatus({ statusApiUrl: STATUS_URL, now: () => Date.parse(NOW) })).generation, 3);
    assert.equal(calls[0].url, `${STATUS_URL}/public/status`);
    assert.equal(calls[0].options.credentials, "omit");
    assert.equal("Authorization" in calls[0].options.headers, false);

    globalThis.fetch = async () => jsonResponse({ ...publicResponse(), tenant_id: "private" });
    await assert.rejects(
      getPublicStatus({ statusApiUrl: STATUS_URL, now: () => Date.parse(NOW) }),
      (error) => error.code === "status.response_invalid",
    );

    globalThis.fetch = async () => jsonResponse(publicResponse());
    await assert.rejects(
      getPublicStatus({ statusApiUrl: STATUS_URL, now: () => Date.parse("2026-08-25T09:00:00Z") }),
      (error) => error.code === "status.snapshot_expired",
    );

    globalThis.fetch = async (url, options) => {
      calls.push({ url: String(url), options });
      return jsonResponse({
        accepted: true,
        request_id: "request-prd112-subscribe",
        trace_id: "trace-prd112-subscribe",
      }, 202);
    };
    await mutatePublicStatusSubscription({
      controlApiUrl: CONTROL_URL,
      operation: "create",
      createIntent: "i".repeat(64),
      contact: "owner@example.invalid",
      scope: { components: ["api-gateway"], regions: [], severities: ["major"] },
    });
    const subscription = calls.at(-1);
    assert.equal(subscription.options.credentials, "omit");
    assert.equal("Authorization" in subscription.options.headers, false);
    assert.deepEqual(JSON.parse(subscription.options.body), {
      operation: "create",
      create_intent: "i".repeat(64),
      channel: "email",
      contact: "owner@example.invalid",
      scope: { components: ["api-gateway"], regions: [], severities: ["major"] },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("platform status clients enforce closed DTOs, CSRF, CAS, and idempotency", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"p".repeat(43)}` };
  try {
    globalThis.fetch = async (url, options) => {
      calls.push({ url: String(url), options });
      return jsonResponse(calls.length === 1 ? platformResponse() : incidentWriteResponse(), 201);
    };
    assert.equal((await getPlatformStatusIncidents({ controlApiUrl: CONTROL_URL })).incidents.length, 1);
    await createPlatformIncident({
      controlApiUrl: CONTROL_URL,
      ownerWorkspaceId: WORKSPACE_ID,
      title: "Gateway request failures",
      classification: "reliability",
      visibility: "public",
      severity: "major",
      acknowledgedAt: NOW,
      initialUpdate: "Gateway request failures are being investigated.",
      affectedComponents: ["api-gateway"],
      affectedRegions: ["cn-north-1"],
      idempotencyKey: "prd112-console-incident-0001",
    });
    await approvePlatformSecurityIncident({
      controlApiUrl: CONTROL_URL,
      incidentId: INCIDENT_ID,
      revision: 1,
      publicMessage: "The security impact is contained and safe to disclose.",
      affectedComponents: ["identity"],
      affectedRegions: [],
      idempotencyKey: "prd112-console-approval-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
  assert.equal(calls[1].options.credentials, "include");
  assert.equal(calls[1].options.cache, "no-store");
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "p".repeat(43));
  assert.equal(calls[1].options.headers["Idempotency-Key"], "prd112-console-incident-0001");
  assert.equal("Authorization" in calls[1].options.headers, false);
  assert.equal(calls[2].options.headers["If-Match"], '"1"');

  const original = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse({ ...platformResponse(), contact: "private" });
  try {
    await assert.rejects(
      getPlatformStatusIncidents({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "status.response_invalid",
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("renderer independently prunes write and security approval controls", () => {
  const readOnly = fakeRoot();
  renderPlatformWorkspace(readOnly, platformState(["platform.status.read"]));
  assert.match(readOnly.innerHTML, /状态与事件/u);
  assert.doesNotMatch(
    readOnly.innerHTML,
    /data-platform-status-create-form|data-platform-maintenance-form|data-platform-status-incident-form/u,
  );

  const approver = fakeRoot();
  renderPlatformWorkspace(approver, platformState([
    "platform.status.read",
    "platform.status.security_approve",
  ]));
  assert.match(approver.innerHTML, /value="security-approval"/u);
  assert.doesNotMatch(
    approver.innerHTML,
    /data-platform-status-create-form|data-platform-maintenance-form/u,
  );

  const writer = fakeRoot();
  renderPlatformWorkspace(writer, platformState([
    "platform.status.read",
    "platform.status.write",
  ]));
  assert.match(writer.innerHTML, /data-platform-status-create-form/u);
  assert.match(writer.innerHTML, /data-platform-maintenance-form/u);
  assert.doesNotMatch(writer.innerHTML, /value="security-approval"/u);
  assert.doesNotMatch(writer.innerHTML, /tenant_id|contact|internal_endpoint/iu);
});

test("controller locks navigation during publication and prunes revoked navigation", async () => {
  const calls = [];
  let permissions = ["platform.status.read", "platform.status.write"];
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const api = controllerApi(calls, () => permissions);
  api.createStatusIncident = async () => {
    calls.push("write");
    await pending;
    return incidentWriteResponse();
  };
  const controller = createPlatformPageController({
    root: fakeRoot(),
    api,
    newIdempotencyKey: () => "prd112-controller-incident-0001",
  });
  assert.equal(await controller.mount(), true);
  controller.selectView("status-incidents");
  const writing = controller.createStatusIncident({
    ownerWorkspaceId: WORKSPACE_ID,
    title: "Gateway request failures",
    classification: "reliability",
    visibility: "public",
    severity: "major",
    acknowledgedAt: NOW,
    initialUpdate: "Gateway request failures are being investigated.",
    affectedComponents: ["api-gateway"],
    affectedRegions: [],
    confirmed: true,
  });
  assert.equal(controller.selectView("overview"), false);
  assert.equal(await controller.refresh(), false);
  release();
  assert.equal(await writing, true);
  assert.equal(calls.filter((item) => item === "status").length, 2);

  permissions = [];
  assert.equal(await controller.refresh(), true);
  assert.equal(await controller.createStatusIncident({ confirmed: true }), false);
  assert.equal(calls.filter((item) => item === "write").length, 1);
});

test("status layouts collapse without browser storage or horizontal tables", async () => {
  const platformCss = await readFile(
    new URL("../../src/styles/platform.css", import.meta.url),
    "utf8",
  );
  const publicCss = await readFile(new URL("../../src/styles/status.css", import.meta.url), "utf8");
  const platformClient = await readFile(
    new URL("../../src/lib/statusIncidents.js", import.meta.url),
    "utf8",
  );
  const publicClient = await readFile(
    new URL("../../src/lib/publicStatus.js", import.meta.url),
    "utf8",
  );
  assert.match(platformCss, /\.platform-status-component-table \{ min-width: 0; \}/u);
  assert.match(platformCss, /\.platform-status-component-table thead \{ display: none; \}/u);
  assert.match(platformCss, /\.platform-status-component-table td::before \{ content: attr\(data-label\)/u);
  assert.match(publicCss, /@media \(max-width: 680px\)[\s\S]*\.status-component-list \{ grid-template-columns: 1fr; \}/u);
  assert.doesNotMatch(
    `${platformClient}\n${publicClient}`,
    /localStorage|sessionStorage|indexedDB/iu,
  );
});

function controllerApi(calls, permissionSource) {
  return {
    session: async () => ({ context: {
      permissions: permissionSource(),
      operator: { display_name: "Operator", roles: ["platform_ops"] },
    } }),
    navigation: async () => ({ items: permissionSource().includes("platform.status.read") ? [{
      id: "status-incidents",
      label: "Status and incidents",
      href: "/platform.html#status-incidents",
    }] : [] }),
    statusIncidents: async () => { calls.push("status"); return platformResponse(); },
  };
}

function platformState(permissions) {
  return {
    session: { context: {
      permissions,
      operator: { display_name: "Operator", roles: ["platform_ops"] },
    } },
    navigation: [{
      id: "status-incidents",
      label: "Status and incidents",
      href: "#status-incidents",
    }],
    view: "status-incidents",
    statusIncidents: platformResponse(),
    statusIncidentIntent: null,
    loading: false,
  };
}

function component() {
  return {
    code: "api-gateway",
    name: "API Gateway",
    group: "gateway",
    status: "operational",
    revision: 1,
  };
}

function update({ publicShape = false } = {}) {
  return {
    ...(publicShape ? {} : { incident_update_id: UPDATE_ID }),
    status: "published",
    message: "Gateway request failures are being investigated.",
    affected_components: ["api-gateway"],
    affected_regions: ["cn-north-1"],
    published_at: NOW,
  };
}

function platformIncident() {
  return {
    incident_id: INCIDENT_ID,
    reference: "INC-20260825-ABC123",
    title: "Security gateway incident",
    classification: "security",
    visibility: "public",
    severity: "major",
    status: "pending_security_review",
    updates: [],
    postmortem: null,
    security_approval_available: true,
    revision: 1,
    acknowledged_at: NOW,
    updated_at: NOW,
  };
}

function platformResponse() {
  return {
    components: [component()],
    incidents: [platformIncident()],
    maintenance_windows: [],
    bundle_generation: null,
    request_id: "request-prd112-platform",
    trace_id: "trace-prd112-platform",
  };
}

function incidentWriteResponse() {
  return {
    incident: {
      ...platformIncident(),
      classification: "reliability",
      status: "published",
      updates: [update()],
      security_approval_available: false,
    },
    idempotent_replay: false,
    request_id: "request-prd112-write",
    trace_id: "trace-prd112-write",
  };
}

function publicResponse() {
  return {
    wire_schema_version: 1,
    generation: 3,
    updated_at: NOW,
    expires_at: "2026-08-25T09:00:00Z",
    overall_status: "degraded_performance",
    components: [component()],
    incidents: [{
      reference: "INC-20260825-ABC123",
      title: "Gateway request failures",
      severity: "major",
      status: "published",
      acknowledged_at: NOW,
      updated_at: NOW,
      updates: [update({ publicShape: true })],
    }],
    maintenance_windows: [],
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
