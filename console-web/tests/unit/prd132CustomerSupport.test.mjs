import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import {
  createSupportTicketPageState,
  supportTicketPageOwner,
} from "../../src/features/support-tickets/supportTicketPageState.js";
import { renderSupportTickets } from "../../src/features/support-tickets/renderSupportTickets.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  attachSupportFile,
  createSupportTicket,
  escalateSupportTicket,
  listSupportTickets,
  patchSupportTicket,
} from "../../src/lib/customerSupport.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";

const CONTROL_URL = "http://control.local";
const TICKET_ID = `tkt_${"1".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"2".repeat(32)}`;
const OPERATOR_ID = `pto_${"3".repeat(32)}`;
const FILE_ID = `file_${"4".repeat(32)}`;
const ATTACHMENT_ID = `att_${"5".repeat(32)}`;
const ESCALATION_ID = `esc_${"6".repeat(32)}`;
const MESSAGE_ID = `msg_${"7".repeat(32)}`;
const REQUEST_ID = "req_prd132_console";
const TRACE_ID = "trc_prd132_console";
const NOW = "2026-08-29T08:00:00.000Z";
const WORKSPACE_CSRF = "W".repeat(43);
const PLATFORM_CSRF = "P".repeat(43);
const IDEMPOTENCY = "support-console-test-000001";

function currentWorkspaceSupportShell(codes = [PERMISSIONS.SUPPORT_TICKET_READ, PERMISSIONS.SUPPORT_TICKET_CREATE]) {
  const identity = { account: { status: "active" }, current_session: {
    session_id: "current_support_session", is_current: true, revoke_generation: 1, expires_at: "2099-01-01T00:00:00Z",
  } };
  const context = { workspace_id: WORKSPACE_ID, role: "developer", session_status: "active", expires_at: "2099-01-01T00:00:00Z" };
  const authorization = { permission_codes: [...codes].sort(), permission_revision: 1, membership_revision: 1, permission_fingerprint: "a".repeat(64) };
  return { state: "ok", workspaceNavigationContract: true, workspaceIdentity: identity,
    workspaceContext: context, workspaceAuthorization: authorization,
    workspaceOwner: workspaceNavigationOwner(identity, { context, authorization }),
    activeNavigationItemId: "support-tickets", activeRouteCode: "workspace.observability.support",
    navigation: { ...authorization, active_navigation_item_id: "support-tickets", active_route_code: "workspace.observability.support",
      navigation_items: [{ id: "support-tickets", route_code: "workspace.observability.support", target_section: "support-tickets", visible: true, disabled: false }] } };
}

test("current Workspace support renders populated tickets and actions without legacy identity fields", () => {
  const shell = currentWorkspaceSupportShell();
  const auth = shell.workspaceAuthorization;
  const owner = supportTicketPageOwner(auth, shell);
  assert.equal(owner.workspaceId, WORKSPACE_ID);
  assert.equal(owner.canCreate, true);
  assert.equal(owner.canManage, false);
  const state = createSupportTicketPageState();
  state.enter(owner);
  const read = state.beginList(owner);
  assert.equal(state.acceptList(read, ticketList("workspace"), owner), true);
  state.finish(read);
  const html = renderSupportTickets(state.snapshot(), auth, shell);
  assert.match(html, /Gateway unavailable/u);
  assert.match(html, /data-support-ticket-create-form/u);
  assert.match(html, /data-support-ticket-reply-form/u);
  assert.equal(Object.hasOwn(auth, "user"), false);
  assert.equal(Object.hasOwn(owner, "tenantId"), false);
});

test("current Workspace support denies expired, mismatched and revoked authorization", () => {
  for (const change of [
    s => { s.workspaceContext.expires_at = "2000-01-01T00:00:00Z"; },
    s => { s.workspaceIdentity.current_session.revoke_generation += 1; },
    s => { s.navigation.permission_fingerprint = "b".repeat(64); },
    s => { s.activeNavigationItemId = "overview"; },
    s => { s.navigation.navigation_items[0].disabled = true; },
  ]) {
    const shell = currentWorkspaceSupportShell();
    change(shell);
    assert.equal(supportTicketPageOwner(shell.workspaceAuthorization, shell), null);
  }
  const shell = currentWorkspaceSupportShell();
  assert.equal(supportTicketPageOwner({ ...shell.workspaceAuthorization, membership_revision: 2 }, shell), null);
  const denied = currentWorkspaceSupportShell([]);
  assert.equal(supportTicketPageOwner(denied.workspaceAuthorization, denied), null);
  const readOnly = currentWorkspaceSupportShell([PERMISSIONS.SUPPORT_TICKET_READ]);
  const owner = supportTicketPageOwner(readOnly.workspaceAuthorization, readOnly);
  const state = createSupportTicketPageState();
  state.enter(owner);
  assert.equal(state.beginMutation(owner), null);
  assert.doesNotMatch(renderSupportTickets(state.snapshot(), readOnly.workspaceAuthorization, readOnly), /data-support-ticket-create-form/u);
});

test("current Workspace support drops late mutations after real session owner changes", () => {
  const shell = currentWorkspaceSupportShell();
  const owner = supportTicketPageOwner(shell.workspaceAuthorization, shell);
  const state = createSupportTicketPageState();
  state.enter(owner);
  const mutation = state.beginMutation(owner);
  const next = currentWorkspaceSupportShell();
  next.workspaceIdentity.current_session.session_id = "replacement_session";
  next.workspaceOwner = workspaceNavigationOwner(next.workspaceIdentity, { context: next.workspaceContext, authorization: next.workspaceAuthorization });
  const nextOwner = supportTicketPageOwner(next.workspaceAuthorization, next);
  state.enter(nextOwner);
  assert.equal(mutation.signal.aborted, true);
  assert.equal(state.acceptTicket(mutation, ticketMutation("workspace"), nextOwner, "support.ticket_updated"), false);
  assert.equal(state.snapshot().items.length, 0);
});

test("support client keeps Workspace and Platform CSRF domains exact with CAS and idempotency", async (t) => {
  const originalDocument = globalThis.document;
  globalThis.document = {
    cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}; __Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
  };
  t.after(() => { globalThis.document = originalDocument; });
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/support/tickets" && options.method === "POST") {
      return jsonResponse(ticketMutation("workspace"));
    }
    if (path === `/support/tickets/${TICKET_ID}`) {
      return jsonResponse(ticketMutation("platform", supportTicket({ revision: 2 })));
    }
    if (path.endsWith("/attachments")) return jsonResponse(attachmentMutation());
    if (path.endsWith("/escalations")) return jsonResponse(escalationMutation());
    return jsonResponse(ticketList(options.headers["X-CSRF-Token"] ? "platform" : "workspace"));
  });

  await listSupportTickets({ controlApiUrl: CONTROL_URL, actorDomain: "workspace" });
  await createSupportTicket({
    controlApiUrl: CONTROL_URL,
    category: "reliability",
    severity: "p1",
    subject: "Gateway unavailable",
    message: "Requests are failing.",
    relatedKind: "request",
    relatedReference: "req_customer_opaque",
    idempotencyKey: IDEMPOTENCY,
  });
  await patchSupportTicket({
    controlApiUrl: CONTROL_URL,
    actorDomain: "platform",
    ticketId: TICKET_ID,
    expectedRevision: 1,
    changes: { message: "Investigating internally.", message_visibility: "internal" },
    idempotencyKey: IDEMPOTENCY,
  });
  await attachSupportFile({
    controlApiUrl: CONTROL_URL,
    ticketId: TICKET_ID,
    expectedRevision: 1,
    fileId: FILE_ID,
    intentCode: "log_bundle",
    idempotencyKey: IDEMPOTENCY,
  });
  await escalateSupportTicket({
    controlApiUrl: CONTROL_URL,
    actorDomain: "platform",
    ticketId: TICKET_ID,
    expectedRevision: 2,
    reasonCode: "platform_manual_review",
    idempotencyKey: IDEMPOTENCY,
  });

  assert.equal(calls.length, 5);
  calls.forEach(({ options }) => {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal("Authorization" in options.headers, false);
  });
  assert.equal(calls[1].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[3].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[4].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[2].options.headers["If-Match"], '"1"');
  assert.equal(calls[4].options.headers["If-Match"], '"2"');
  assert.equal(calls[1].options.headers["Idempotency-Key"], IDEMPOTENCY);
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    file_id: FILE_ID,
    intent_code: "log_bundle",
    confirmed: true,
  });
});

test("support client rejects response expansion, owner drift, and Workspace internal notes", async (t) => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}` };
  t.after(() => { globalThis.document = originalDocument; });
  installFetch(t, () => jsonResponse({ ...ticketList("workspace"), object_url: "forbidden" }));
  await assert.rejects(
    () => listSupportTickets({ controlApiUrl: CONTROL_URL, actorDomain: "workspace" }),
    (error) => error instanceof AuthApiError && error.code === "support.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(ticketList("platform"));
  await assert.rejects(
    () => listSupportTickets({ controlApiUrl: CONTROL_URL, actorDomain: "workspace" }),
    (error) => error instanceof AuthApiError && error.code === "support.response_invalid",
  );
  await assert.rejects(
    () => patchSupportTicket({
      controlApiUrl: CONTROL_URL,
      actorDomain: "workspace",
      ticketId: TICKET_ID,
      expectedRevision: 1,
      changes: { message: "hidden", message_visibility: "internal" },
      idempotencyKey: IDEMPOTENCY,
    }),
    (error) => error instanceof AuthApiError && error.code === "support.request_invalid",
  );
});

test("Workspace support state rejects stale responses and clears private facts on owner drift", () => {
  const auth = workspaceContext("support-owner-a", [
    PERMISSIONS.SUPPORT_TICKET_READ,
    PERMISSIONS.SUPPORT_TICKET_CREATE,
    PERMISSIONS.SUPPORT_TICKET_MANAGE,
  ]);
  const shell = workspaceShell(auth);
  const owner = supportTicketPageOwner(auth, shell);
  const state = createSupportTicketPageState();
  assert.equal(state.enter(owner), true);
  const stale = state.beginList(owner);
  const current = state.beginList(owner);
  assert.equal(stale.signal.aborted, true);
  assert.equal(state.acceptList(stale, ticketList("workspace"), owner), false);
  assert.equal(state.acceptList(current, ticketList("workspace"), owner), true);
  state.finish(current);
  assert.equal(state.snapshot().items[0].subject, "Gateway unavailable");

  const mutation = state.beginMutation(owner, { ticketId: TICKET_ID });
  const nextAuth = workspaceContext("support-owner-b", [PERMISSIONS.SUPPORT_TICKET_READ]);
  const nextOwner = supportTicketPageOwner(nextAuth, workspaceShell(nextAuth));
  assert.equal(state.enter(nextOwner), true);
  assert.equal(mutation.signal.aborted, true);
  assert.equal(state.snapshot().items.length, 0);
  assert.equal(state.acceptTicket(mutation, ticketMutation("workspace"), nextOwner, "support.ticket_updated"), false);
});

test("Workspace and Platform render the complete support workflow without storage references or object locations", async () => {
  const auth = workspaceContext("support-render", [
    PERMISSIONS.SUPPORT_TICKET_READ,
    PERMISSIONS.SUPPORT_TICKET_CREATE,
    PERMISSIONS.SUPPORT_TICKET_MANAGE,
  ]);
  const shell = workspaceShell(auth);
  const owner = supportTicketPageOwner(auth, shell);
  const state = createSupportTicketPageState();
  state.enter(owner);
  const operation = state.beginList(owner);
  state.acceptList(operation, ticketList("workspace", [supportTicket({
    attachments: [attachment({ status: "scan_timeout", scan_outcome: "timeout" })],
  })]), owner);
  state.finish(operation);
  const workspaceHtml = renderSupportTickets(state.snapshot(), auth, shell);
  assert.match(workspaceHtml, /客户支持/u);
  assert.match(workspaceHtml, /扫描超时/u);
  assert.match(workspaceHtml, /data-support-ticket-reply-form/u);
  assert.match(workspaceHtml, /data-support-ticket-survey-form/u);
  assert.doesNotMatch(workspaceHtml, new RegExp(`${FILE_ID}|object_url|storage_path|Authorization|Bearer`, "iu"));

  const root = { innerHTML: "" };
  renderPlatformWorkspace(root, platformState());
  assert.match(root.innerHTML, /支持授权与协作/u);
  assert.match(root.innerHTML, /内部备注/u);
  assert.match(root.innerHTML, /data-platform-support-sla-form/u);
  assert.match(root.innerHTML, /申请诊断访问/u);
  assert.doesNotMatch(root.innerHTML, new RegExp(`${FILE_ID}|object_url|storage_path|Authorization|Bearer`, "iu"));

  const [client, pageState, binding, main, platformStateSource, platformRender, css, platformCss] = await Promise.all([
    readFile(new URL("../../src/lib/customerSupport.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/support-tickets/supportTicketPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/support-tickets/bindSupportTickets.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/renderPlatformPage.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(`${client}\n${pageState}\n${binding}`, /localStorage|sessionStorage|indexedDB/iu);
  assert.doesNotMatch(client, /Authorization|Bearer/iu);
  assert.match(binding, /textarea[\s\S]*input:not/u);
  assert.match(main, /case "support-tickets":[\s\S]*loadSupportTicketsState/u);
  assert.match(main, /nextItemId !== "support-tickets"[\s\S]*supportTicketPageState\.clear/u);
  assert.match(main, /function renderLogin[\s\S]*supportTicketPageState\.clear/u);
  assert.match(main, /clearSupportTicketStateOnIdentityChange/u);
  assert.match(platformStateSource, /state\.view === "support"[\s\S]*loadSupportTickets/u);
  assert.match(platformRender, /message\.visibility|item\.visibility/u);
  assert.match(css, /@media \(max-width: 420px\)[\s\S]*\.support-ticket/u);
  assert.match(platformCss, /@media \(max-width: 600px\)[\s\S]*\.platform-support/u);
});

test("Platform support controller drops late queues and commits only current internal collaboration", async () => {
  let deferQueue = false;
  let releaseQueue;
  const root = createRoot();
  const api = {
    session: async () => platformState().session,
    navigation: async () => ({
      items: [
        { id: "support", label: "Support", href: "/platform.html#support" },
        { id: "overview", label: "Overview", href: "/platform.html#overview" },
      ],
      request_id: REQUEST_ID,
      trace_id: TRACE_ID,
    }),
    grants: async () => ({ grants: [], request_id: REQUEST_ID, trace_id: TRACE_ID }),
    supportTickets: async () => {
      if (!deferQueue) return ticketList("platform");
      return new Promise((resolve) => { releaseQueue = resolve; });
    },
    patchSupportTicket: async ({ changes }) => ticketMutation("platform", supportTicket({
      revision: 2,
      status: "triaged",
      messages: [
        ...supportTicket().messages,
        {
          message_id: `msg_${"8".repeat(32)}`,
          author_domain: "platform",
          visibility: changes.message_visibility,
          body: changes.message,
          created_at: NOW,
        },
      ],
    })),
    escalateSupportTicket: async () => escalationMutation(),
    supportIdempotencyKey: () => IDEMPOTENCY,
  };
  const controller = createPlatformPageController({ root, api });
  assert.equal(await controller.mount(), true);
  controller.selectView("support");
  assert.equal(await controller.filterSupportTickets({ status: "", severity: "" }), true);
  assert.match(root.innerHTML, /Gateway unavailable/u);
  assert.equal(await controller.mutateSupportTicket({
    ticketId: TICKET_ID,
    expectedRevision: 1,
    changes: { message: "Check regional cell.", message_visibility: "internal" },
    feedback: "内部备注已保存。",
  }), true);
  assert.match(root.innerHTML, /Check regional cell\./u);
  assert.match(root.innerHTML, /内部备注已保存/u);

  deferQueue = true;
  const stale = controller.filterSupportTickets({ status: "open" });
  while (!releaseQueue) await Promise.resolve();
  controller.selectView("overview");
  releaseQueue(ticketList("platform", [supportTicket({ subject: "Stale private subject" })]));
  assert.equal(await stale, false);
  assert.doesNotMatch(root.innerHTML, /Stale private subject/u);
  controller.destroy();
  assert.equal(root.innerHTML, "");
});

function ticketList(actorDomain, items = [supportTicket()]) {
  return { actor_domain: actorDomain, items, request_id: REQUEST_ID, trace_id: TRACE_ID };
}
function ticketMutation(actorDomain, ticket = supportTicket()) {
  return { actor_domain: actorDomain, ticket, idempotent_replay: false, request_id: REQUEST_ID, trace_id: TRACE_ID };
}
function attachmentMutation() {
  return { attachment: attachment(), ticket_revision: 2, idempotent_replay: false, request_id: REQUEST_ID, trace_id: TRACE_ID };
}
function escalationMutation() {
  return { escalation: escalation(), ticket_revision: 3, idempotent_replay: false, request_id: REQUEST_ID, trace_id: TRACE_ID };
}
function supportTicket(overrides = {}) {
  return {
    ticket_id: TICKET_ID,
    workspace_id: WORKSPACE_ID,
    category: "reliability",
    severity: "p1",
    status: "resolved",
    subject: "Gateway unavailable",
    reported_by_me: true,
    assignee_operator_id: OPERATOR_ID,
    revision: 1,
    last_customer_activity_at: NOW,
    last_platform_activity_at: NOW,
    resolved_at: NOW,
    closed_at: null,
    created_at: NOW,
    updated_at: NOW,
    messages: [{ message_id: MESSAGE_ID, author_domain: "customer", visibility: "customer", body: "Requests are failing.", created_at: NOW }],
    attachments: [],
    sla: {
      state: "stopped", generation: 1, first_response_deadline_at: NOW,
      resolution_deadline_at: NOW, risk_at: NOW, first_responded_at: NOW,
      risk_generation: 1, escalation_generation: 1, revision: 2,
    },
    escalations: [],
    survey: null,
    ...overrides,
  };
}
function attachment(overrides = {}) {
  return {
    attachment_id: ATTACHMENT_ID,
    intent_code: "log_bundle",
    media_type: "text/plain",
    size_bytes: 2048,
    status: "quarantined",
    scan_version: "ticket-attachment-scan-v1",
    scan_outcome: null,
    revision: 1,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}
function escalation() {
  return { escalation_id: ESCALATION_ID, source_code: "platform_manual", reason_code: "platform_manual_review", generation: 1, status: "open", created_at: NOW };
}
function workspaceContext(fingerprint, permissionCodes) {
  return withPermissionSnapshot({
    user: { tenant_id: "11111111-1111-4111-8111-111111111111", account_id: "gac_prd132", user_id: "usr_prd132", role: "admin" },
    tenant: { tenant_id: "11111111-1111-4111-8111-111111111111" },
    session: { session_id: "ses_prd132", session_revision: 1 },
  }, permissionCodes, { authorizationFingerprint: fingerprint });
}
function workspaceShell(auth) {
  return {
    state: "ok",
    activeNavigationItemId: "support-tickets",
    navigation: {
      active_navigation_item_id: "support-tickets",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [{ id: "support-tickets", target_section: "support-tickets", required_permissions: [PERMISSIONS.SUPPORT_TICKET_READ], visible: true, disabled: false }],
    },
  };
}
function platformState() {
  const platformTicket = supportTicket({
    status: "triaged",
    messages: [
      { message_id: MESSAGE_ID, author_domain: "customer", visibility: "customer", body: "Requests are failing.", created_at: NOW },
      { message_id: `msg_${"8".repeat(32)}`, author_domain: "platform", visibility: "internal", body: "Check regional cell.", created_at: NOW },
    ],
    attachments: [attachment({ status: "scan_timeout", scan_outcome: "timeout" })],
    escalations: [escalation()],
    sla: { ...supportTicket().sla, state: "running" },
  });
  return {
    loading: false,
    view: "support",
    session: {
      context: {
        operator: { operator_id: OPERATOR_ID, display_name: "Support Operator", roles: ["platform_support"] },
        permissions: ["platform.support.ticket.read", "platform.support.ticket.manage", "platform.support.sla.manage", "platform.support.request"],
      },
    },
    navigation: [{ id: "support", label: "Support", href: "/platform.html#support" }],
    supportTickets: ticketList("platform", [platformTicket]),
    supportTicketFilters: { status: "", severity: "", workspaceId: "" },
    selectedSupportTicketId: TICKET_ID,
    grants: { grants: [] },
    diagnostics: null,
    feedback: null,
    error: null,
    pendingAction: null,
  };
}
function installFetch(t, responder) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url: String(url), options }); return responder(url, options); };
  t.after(() => { globalThis.fetch = original; });
  return calls;
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
function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}
