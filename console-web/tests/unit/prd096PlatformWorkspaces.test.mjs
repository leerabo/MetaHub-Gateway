import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  addPlatformWorkspaceRiskTag,
  createPlatformWorkspaceOwnerNotification,
  getPlatformWorkspace,
  getPlatformWorkspaceHealth,
  listPlatformWorkspaceEvents,
  listPlatformWorkspaces,
  removePlatformWorkspaceRiskTag,
  restorePlatformWorkspace,
  suspendPlatformWorkspace,
} from "../../src/lib/platform.js";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"3".repeat(32)}`;
const OTHER_WORKSPACE_ID = `wsp_${"4".repeat(32)}`;
const OPERATOR_ID = `pto_${"2".repeat(32)}`;
const PLATFORM_CSRF = "P".repeat(43);

test("PRD-096 clients keep Workspace reads and CAS writes on the Platform credential domain", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${PLATFORM_CSRF}` };
  const calls = installFetch(() => jsonResponse({ request_id: "req_ok", trace_id: "trc_ok" }));
  try {
    await listPlatformWorkspaces({
      controlApiUrl: CONTROL_URL,
      filters: {
        search: WORKSPACE_ID,
        operational_status: "active",
        region: "cn-north-1",
        risk_tag: "security_review",
        health_status: "fresh",
        health_risk: "healthy",
      },
      cursor: "payload.signature",
      limit: 50,
    });
    await getPlatformWorkspace({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID });
    await getPlatformWorkspaceHealth({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID });
    await listPlatformWorkspaceEvents({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID });
    await suspendPlatformWorkspace({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      revision: 3,
      reasonCode: "security_containment",
      idempotencyKey: "workspace:suspend:0001",
    });
    await restorePlatformWorkspace({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      revision: 4,
      reasonCode: "security_cleared",
      idempotencyKey: "workspace:restore:0001",
    });
    await addPlatformWorkspaceRiskTag({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      revision: 7,
      tagCode: "security_review",
      reasonCode: "security_review",
      idempotencyKey: "workspace:risk:add:0001",
    });
    await removePlatformWorkspaceRiskTag({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      revision: 8,
      tagCode: "security_review",
      reasonCode: "security_review",
      idempotencyKey: "workspace:risk:remove:0001",
    });
    await createPlatformWorkspaceOwnerNotification({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      revision: 5,
      templateCode: "workspace_operational_notice",
      reasonCode: "operational_notice",
      idempotencyKey: "workspace:owner:notice:0001",
    });
  } finally {
    globalThis.document = originalDocument;
  }

  const listUrl = new URL(calls[0].url);
  assert.equal(listUrl.pathname, "/platform/workspaces");
  assert.deepEqual(Object.fromEntries(listUrl.searchParams), {
    limit: "50",
    cursor: "payload.signature",
    health_risk: "healthy",
    health_status: "fresh",
    operational_status: "active",
    region: "cn-north-1",
    risk_tag: "security_review",
    search: WORKSPACE_ID,
  });
  assert.deepEqual(calls.map(({ url, options }) => [new URL(url).pathname, options.method]), [
    ["/platform/workspaces", "GET"],
    [`/platform/workspaces/${WORKSPACE_ID}`, "GET"],
    [`/platform/workspaces/${WORKSPACE_ID}/health`, "GET"],
    [`/platform/workspaces/${WORKSPACE_ID}/events`, "GET"],
    [`/platform/workspaces/${WORKSPACE_ID}/suspend`, "POST"],
    [`/platform/workspaces/${WORKSPACE_ID}/restore`, "POST"],
    [`/platform/workspaces/${WORKSPACE_ID}/risk-tags`, "POST"],
    [`/platform/workspaces/${WORKSPACE_ID}/risk-tags/security_review`, "DELETE"],
    [`/platform/workspaces/${WORKSPACE_ID}/owner-notifications`, "POST"],
  ]);
  for (const call of calls.slice(0, 4)) {
    assert.equal(call.options.headers["X-CSRF-Token"], undefined);
  }
  for (const call of calls.slice(4)) {
    assert.equal(call.options.headers["X-CSRF-Token"], PLATFORM_CSRF);
    assert.match(call.options.headers["Idempotency-Key"], /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u);
    assert.equal("Authorization" in call.options.headers, false);
  }
  assert.deepEqual(calls.slice(4).map((call) => call.options.headers["If-Match"]), [
    '"3"',
    '"4"',
    '"7"',
    '"8"',
    '"5"',
  ]);
  assert.deepEqual(JSON.parse(calls[4].options.body), {
    reason_code: "security_containment",
    confirmed: true,
  });
  assert.deepEqual(JSON.parse(calls[8].options.body), {
    template_code: "workspace_operational_notice",
    reason_code: "operational_notice",
  });
});

test("PRD-096 clients reject non-exact search, unsupported filters, and invalid tags before fetch", async () => {
  let fetchCount = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse({ request_id: "req_unused", trace_id: "trc_unused" });
  };
  try {
    assert.throws(
      () => listPlatformWorkspaces({ controlApiUrl: CONTROL_URL, filters: { search: "Acme AI" } }),
      (error) => error.code === "platform.request_invalid",
    );
    assert.throws(
      () => listPlatformWorkspaces({ controlApiUrl: CONTROL_URL, filters: { owner: "someone" } }),
      (error) => error.code === "platform.request_invalid",
    );
    assert.throws(
      () => addPlatformWorkspaceRiskTag({
        controlApiUrl: CONTROL_URL,
        workspaceId: WORKSPACE_ID,
        revision: 1,
        tagCode: "free_text",
        reasonCode: "security_review",
      }),
      (error) => error.code === "platform.request_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(fetchCount, 0);
});

test("PRD-096 controller filters, opens details, and reloads every snapshot after writes", async () => {
  const root = createRoot();
  const seen = [];
  let operationalStatus = "active";
  let operationalRevision = 3;
  let riskRevision = 7;
  let riskTags = [];
  let notificationStatus = null;
  const api = workspaceApi({
    workspaces: async (input) => {
      seen.push(["list", input.filters, input.cursor, input.signal]);
      return workspaceListPayload(workspaceSummary({
        operational_status: operationalStatus,
        operational_revision: operationalRevision,
        risk_revision: riskRevision,
        platform_risk_tags: riskTags,
        last_notification_status: notificationStatus,
      }));
    },
    workspace: async (input) => {
      seen.push(["detail", input.workspaceId, input.signal]);
      return workspaceDetailPayload(workspaceSummary({
        operational_status: operationalStatus,
        operational_revision: operationalRevision,
        risk_revision: riskRevision,
        platform_risk_tags: riskTags,
        last_notification_status: notificationStatus,
      }));
    },
    suspendWorkspace: async (input) => {
      seen.push(["suspend", input]);
      operationalStatus = "suspended";
      operationalRevision += 1;
      return {};
    },
    addWorkspaceRiskTag: async (input) => {
      seen.push(["risk-add", input]);
      riskTags = [input.tagCode];
      riskRevision += 1;
      return {};
    },
    notifyWorkspaceOwner: async (input) => {
      seen.push(["notify", input]);
      notificationStatus = "pending";
      return {};
    },
  });
  const controller = createPlatformPageController({ root, api });

  assert.equal(await controller.mount(), true);
  controller.selectView("workspaces");
  assert.match(root.innerHTML, /Acme AI/u);
  assert.match(root.innerHTML, /data-workspace-filter-form/u);
  assert.equal(await controller.filterWorkspaces({
    search: WORKSPACE_ID,
    operational_status: "active",
    ignored: "not-forwarded",
  }), true);
  assert.deepEqual(seen.at(-1).slice(0, 3), [
    "list",
    { search: WORKSPACE_ID, operational_status: "active" },
    null,
  ]);

  assert.equal(await controller.openWorkspace(WORKSPACE_ID), true);
  assert.match(root.innerHTML, /运营摘要/u);
  assert.match(root.innerHTML, /健康投影/u);
  assert.match(root.innerHTML, /事件时间线/u);
  assert.match(root.innerHTML, /冻结 Workspace/u);

  assert.equal(controller.prepareWorkspaceLifecycle("suspend"), true);
  assert.match(root.innerHTML, /确认冻结 Workspace/u);
  assert.equal(await controller.confirmWorkspaceLifecycle("security_containment"), true);
  const suspend = seen.find((entry) => entry[0] === "suspend")[1];
  assert.equal(suspend.revision, 3);
  assert.equal(suspend.reasonCode, "security_containment");
  assert.equal(suspend.signal.aborted, true);
  assert.match(root.innerHTML, /Workspace 已冻结/u);
  assert.match(root.innerHTML, /恢复 Workspace/u);

  assert.equal(await controller.addWorkspaceRiskTag("security_review", "security_review"), true);
  assert.match(root.innerHTML, /security review/u);
  assert.equal(await controller.notifyWorkspaceOwner(
    "workspace_operational_notice",
    "operational_notice",
  ), true);
  assert.match(root.innerHTML, /pending/u);
  assert.ok(seen.filter((entry) => entry[0] === "list").length >= 5);
  assert.ok(seen.filter((entry) => entry[0] === "detail").length >= 4);
  controller.destroy();
});

test("PRD-096 controller ignores a late Workspace detail snapshot", async () => {
  const root = createRoot();
  let releaseFirst;
  const api = workspaceApi({
    workspace: async ({ workspaceId }) => {
      if (workspaceId === WORKSPACE_ID) {
        return new Promise((resolve) => {
          releaseFirst = () => resolve(workspaceDetailPayload(workspaceSummary({
            workspace_id: WORKSPACE_ID,
            display_name: "Stale Workspace",
          })));
        });
      }
      return workspaceDetailPayload(workspaceSummary({
        workspace_id: OTHER_WORKSPACE_ID,
        display_name: "Current Workspace",
      }));
    },
  });
  const controller = createPlatformPageController({ root, api });
  assert.equal(await controller.mount(), true);
  controller.selectView("workspaces");
  const stale = controller.openWorkspace(WORKSPACE_ID);
  while (!releaseFirst) await Promise.resolve();
  assert.equal(await controller.openWorkspace(OTHER_WORKSPACE_ID), true);
  releaseFirst();
  assert.equal(await stale, false);
  assert.match(root.innerHTML, /Current Workspace/u);
  assert.doesNotMatch(root.innerHTML, /Stale Workspace/u);
  controller.destroy();
});

test("PRD-096 renderer keeps role-specific controls and mobile rows bounded", async () => {
  const supportRoot = createRoot();
  renderPlatformWorkspace(supportRoot, detailState(["platform.workspace.directory.read"], {
    platform_risk_tags: undefined,
    owner_contact_status: undefined,
  }));
  assert.doesNotMatch(supportRoot.innerHTML, /data-workspace-risk-form/u);
  assert.doesNotMatch(supportRoot.innerHTML, /prepare-workspace-lifecycle/u);
  assert.doesNotMatch(supportRoot.innerHTML, /data-workspace-notification-form/u);

  const securityRoot = createRoot();
  renderPlatformWorkspace(securityRoot, detailState([
    "platform.workspace.directory.read",
    "platform.workspace.risk.manage",
  ], { platform_risk_tags: ["security_review"] }));
  assert.match(securityRoot.innerHTML, /value="security_review"/u);
  assert.match(securityRoot.innerHTML, /value="compliance_hold"/u);
  assert.doesNotMatch(securityRoot.innerHTML, /value="payment_attention"/u);
  assert.doesNotMatch(securityRoot.innerHTML, /value="adoption_attention"/u);

  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  assert.match(css, /\.platform-workspace-table \{ min-width: 0; \}/u);
  assert.match(css, /content: attr\(data-label\)/u);
  assert.match(css, /grid-template-columns: 108px minmax\(0,1fr\)/u);
  assert.match(css, /\.platform-filter-form input,[\s\S]*min-width: 0; width: 100%;/u);
  assert.match(css, /\.platform-confirm button\.platform-primary \{[^}]*background: var\(--green\);[^}]*color: white;/u);
  assert.match(css, /\.platform-confirm button\.platform-danger \{[^}]*background: var\(--red\);[^}]*color: white;/u);
  const controllerSource = await readFile(
    new URL("../../src/features/platform/platformPageState.js", import.meta.url),
    "utf8",
  );
  assert.match(controllerSource, /scrollIntoView\?\.\(\{ block: "nearest", inline: "nearest" \}\)/u);
  assert.doesNotMatch(controllerSource, /localStorage|sessionStorage|history\.|location\./u);
});

function workspaceApi(overrides = {}) {
  return {
    session: async () => sessionPayload(),
    openSession: async () => sessionPayload(),
    navigation: async () => navigationPayload(),
    operators: async () => ({ operators: [], total: 0 }),
    grants: async () => ({ grants: [], total: 0 }),
    audit: async () => ({ entries: [], total: 0 }),
    workspaces: async () => workspaceListPayload(),
    workspace: async ({ workspaceId }) => workspaceDetailPayload(workspaceSummary({ workspace_id: workspaceId })),
    workspaceHealth: async ({ workspaceId }) => workspaceHealthPayload(workspaceId),
    workspaceEvents: async () => workspaceEventsPayload(),
    suspendWorkspace: async () => ({}),
    restoreWorkspace: async () => ({}),
    addWorkspaceRiskTag: async () => ({}),
    removeWorkspaceRiskTag: async () => ({}),
    notifyWorkspaceOwner: async () => ({}),
    ...overrides,
  };
}

function sessionPayload(permissions = [
  "platform.workspace.directory.read",
  "platform.workspace.lifecycle.manage",
  "platform.workspace.risk.manage",
  "platform.customer_success.read",
]) {
  return {
    context: {
      operator: {
        operator_id: OPERATOR_ID,
        display_name: "Platform Owner",
        status: "active",
        roles: ["platform_owner"],
        revision: 1,
      },
      permissions,
      expires_at: "2026-08-23T12:00:00Z",
    },
    request_id: "req_session",
    trace_id: "trc_session",
  };
}

function navigationPayload() {
  return {
    items: [{ id: "workspaces", label: "Workspaces", href: "/platform.html#workspaces" }],
    request_id: "req_navigation",
    trace_id: "trc_navigation",
  };
}

function workspaceSummary(overrides = {}) {
  return {
    workspace_id: WORKSPACE_ID,
    kind: "organization",
    display_name: "Acme AI",
    slug: "acme-ai",
    created_at: "2026-08-20T10:00:00Z",
    operational_status: "active",
    operational_revision: 3,
    risk_revision: 7,
    region_code: "cn-north-1",
    residency_policy_version: "cn-v1",
    member_count: 4,
    active_session_count: 2,
    plan_status: "not_available",
    deployment_status: "not_configured",
    data_lifecycle_status: "active",
    latest_export_status: null,
    latest_deletion_status: null,
    gateway_status: "not_applicable",
    health: healthSummary(),
    platform_risk_tags: [],
    owner_contact_status: "available",
    last_notification_status: null,
    updated_at: "2026-08-22T10:00:00Z",
    ...overrides,
  };
}

function workspaceListPayload(summary = workspaceSummary()) {
  return {
    items: [summary],
    limit: 50,
    has_more: false,
    next_cursor: null,
    request_id: "req_workspaces",
    trace_id: "trc_workspaces",
  };
}

function workspaceDetailPayload(summary = workspaceSummary()) {
  return {
    workspace: summary,
    recent_actions: [],
    request_id: "req_workspace",
    trace_id: "trc_workspace",
  };
}

function healthSummary() {
  return {
    status: "fresh",
    score_band: "healthy",
    risk_level: "healthy",
    risk_tags: [],
    evaluated_at: "2026-08-22T10:00:00Z",
  };
}

function workspaceHealthPayload(workspaceId = WORKSPACE_ID) {
  return {
    workspace_id: workspaceId,
    health: healthSummary(),
    request_id: "req_health",
    trace_id: "trc_health",
  };
}

function workspaceEventsPayload() {
  return {
    events: [],
    limit: 50,
    has_more: false,
    request_id: "req_events",
    trace_id: "trc_events",
  };
}

function detailState(permissions, workspaceOverrides = {}) {
  return {
    loading: false,
    session: sessionPayload(permissions),
    navigation: navigationPayload().items,
    workspaces: workspaceListPayload(),
    workspaceFilters: {},
    workspaceCursorHistory: [],
    selectedWorkspaceId: WORKSPACE_ID,
    workspaceDetail: workspaceDetailPayload(workspaceSummary(workspaceOverrides)),
    workspaceHealth: workspaceHealthPayload(),
    workspaceEvents: workspaceEventsPayload(),
    view: "workspaces",
    error: null,
    feedback: null,
    pendingAction: null,
  };
}

function createRoot() {
  let html = "";
  return {
    get innerHTML() { return html; },
    set innerHTML(value) { html = String(value); },
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { html = ""; },
  };
}

function installFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return handler(String(url), options);
  };
  return calls;
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(payload); },
  };
}
