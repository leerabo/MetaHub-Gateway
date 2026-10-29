import assert from "node:assert/strict";
import test from "node:test";

import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  createPlatformNotificationTemplate,
  getWorkspaceNotificationPreferences,
  listPlatformNotificationTemplates,
  listWorkspaceNotifications,
  markWorkspaceNotificationRead,
  updateWorkspaceNotificationPreferences,
} from "../../src/lib/notifications.js";

const CONTROL_URL = "http://control.test";
const NOTIFICATION_ID = `ntf_${"1".repeat(32)}`;
const TEMPLATE_ID = `ntp_${"2".repeat(32)}`;
const WORKSPACE_CSRF = "w".repeat(43);
const PLATFORM_CSRF = "p".repeat(43);
const NOW = "2026-08-23T08:00:00Z";

test("notification clients keep Workspace and Platform credential domains exact", async () => {
  const originalDocument = globalThis.document;
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.document = {
    cookie: [
      `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}`,
      `__Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
    ].join("; "),
  };
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    const path = new URL(url).pathname;
    if (path === "/notifications" && options.method !== "PATCH") {
      return jsonResponse(notificationList());
    }
    if (path.endsWith("/read")) return jsonResponse(notificationRead());
    if (path === "/notification-preferences") return jsonResponse(preferences());
    if (path === "/platform/notification-templates" && options.method === "POST") {
      return jsonResponse(templateResponse(), 201);
    }
    return jsonResponse(templateList());
  };
  try {
    await listWorkspaceNotifications({
      controlApiUrl: CONTROL_URL,
      unreadOnly: true,
      limit: 30,
    });
    await markWorkspaceNotificationRead({
      controlApiUrl: CONTROL_URL,
      notificationId: NOTIFICATION_ID,
    });
    await getWorkspaceNotificationPreferences({ controlApiUrl: CONTROL_URL });
    await updateWorkspaceNotificationPreferences({
      controlApiUrl: CONTROL_URL,
      revision: 1,
      changes: [{ category: "billing", channel: "email", enabled: false }],
    });
    await listPlatformNotificationTemplates({ controlApiUrl: CONTROL_URL });
    await createPlatformNotificationTemplate({
      controlApiUrl: CONTROL_URL,
      revision: 1,
      idempotencyKey: "prd105-template-client-0001",
      template: templateInput(),
    });
  } finally {
    globalThis.document = originalDocument;
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, `${CONTROL_URL}/notifications?unread_only=true&limit=30`);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.deepEqual(JSON.parse(calls[1].options.body), {});
  assert.equal(calls[2].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[3].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[3].options.headers["If-Match"], '"1"');
  assert.equal(calls[4].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[5].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[5].options.headers["If-Match"], '"1"');
  assert.equal(calls[5].options.headers["Idempotency-Key"], "prd105-template-client-0001");
  assert.equal(calls.every((call) => call.options.credentials === "include"), true);
  assert.equal(calls.every((call) => call.options.cache === "no-store"), true);
  assert.equal(calls.some((call) => "Authorization" in call.options.headers), false);
});

test("notification clients reject unknown response fields and invalid template syntax", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => jsonResponse({ ...notificationList(), address: "hidden" });
    await assert.rejects(
      listWorkspaceNotifications({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "notification.response_invalid",
    );

    assert.throws(
      () => createPlatformNotificationTemplate({
        controlApiUrl: CONTROL_URL,
        revision: 1,
        idempotencyKey: "prd105-template-client-bad1",
        template: { ...templateInput(), body_template: "<b>{{total}}</b>" },
      }),
      (error) => error.code === "notification.request_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("notification renderers escape content and prune controls by live permission", () => {
  const workspace = { innerHTML: "" };
  renderIdentitySecurityPage(workspace, identityState());
  assert.match(workspace.innerHTML, /通知中心/u);
  assert.match(workspace.innerHTML, /3 条未读/u);
  assert.match(workspace.innerHTML, /&lt;script&gt;blocked&lt;\/script&gt;/u);
  assert.doesNotMatch(workspace.innerHTML, /<script>blocked<\/script>/u);
  assert.match(workspace.innerHTML, /系统强制/u);
  assert.match(workspace.innerHTML, /data-notification-category="security"[\s\S]*disabled/u);

  const writable = { innerHTML: "" };
  renderPlatformWorkspace(writable, platformState([
    "platform.notification_template.read",
    "platform.notification_template.write",
  ]));
  assert.match(writable.innerHTML, /通知模板/u);
  assert.match(writable.innerHTML, /data-platform-notification-template-form/u);
  assert.match(writable.innerHTML, /invoice_number_masked/u);

  const readOnly = { innerHTML: "" };
  renderPlatformWorkspace(readOnly, platformState(["platform.notification_template.read"]));
  assert.match(readOnly.innerHTML, /模板版本/u);
  assert.doesNotMatch(readOnly.innerHTML, /data-platform-notification-template-form/u);
});

function notificationList() {
  return {
    notifications: [{
      notification_id: NOTIFICATION_ID,
      event_type: "invoice.issued",
      category: "invoices",
      subject: "Invoice issued",
      body: "<script>blocked</script>",
      read: false,
      created_at: NOW,
    }],
    unread_count: 3,
    next_cursor: null,
    request_id: "req-notifications",
    trace_id: "trc-notifications",
  };
}

function notificationRead() {
  const notification = { ...notificationList().notifications[0], read: true };
  return { notification, request_id: "req-read", trace_id: "trc-read" };
}

function preferences() {
  return {
    preferences: [
      { category: "security", channel: "email", enabled: true, mandatory: true, available: true },
      { category: "billing", channel: "email", enabled: true, mandatory: false, available: true },
    ],
    revision: 1,
    request_id: "req-preferences",
    trace_id: "trc-preferences",
  };
}

function templateInput() {
  return {
    template_code: "invoice_issued",
    event_type: "invoice.issued",
    channel: "email",
    locale: "en",
    parameter_allowlist: ["currency", "invoice_number_masked", "issued_at", "total"],
    subject_template: "Invoice {{invoice_number_masked}} issued",
    body_template: "Invoice {{invoice_number_masked}} for {{total}} {{currency}} was issued at {{issued_at}}.",
    status: "published",
  };
}

function template() {
  return {
    notification_template_id: TEMPLATE_ID,
    ...templateInput(),
    category: "invoices",
    version: 1,
    content_sha256: "a".repeat(64),
    revision: 1,
    created_at: NOW,
    published_at: NOW,
  };
}

function templateList() {
  return {
    templates: [template()],
    catalog_revision: 1,
    request_id: "req-templates",
    trace_id: "trc-templates",
  };
}

function templateResponse() {
  return {
    template: template(),
    catalog_revision: 2,
    idempotent_replay: false,
    request_id: "req-template",
    trace_id: "trc-template",
  };
}

function identityState() {
  return {
    account: { display_name: "User", email: "user@example.com", status: "active", mfa_configured: true },
    currentSession: { session_id: "session", is_current: true },
    sessions: [],
    workspaceChoices: [],
    workspaces: [],
    memberships: [],
    navigationItems: [],
    workspaceContext: { workspace_id: `wsp_${"3".repeat(32)}`, name: "Workspace", role: "owner" },
    notificationCenterOpen: true,
    notificationInbox: notificationList(),
    notificationPreferences: preferences(),
    notificationBusy: false,
    notificationUnreadOnly: false,
    notificationPage: 1,
  };
}

function platformState(permissions) {
  return {
    session: {
      context: {
        permissions,
        operator: { display_name: "Communications", roles: ["platform_communications"] },
      },
    },
    navigation: [{
      id: "notification-templates",
      label: "通知模板",
      href: "/platform.html#notification-templates",
    }],
    view: "notification-templates",
    notificationTemplates: templateList(),
    loading: false,
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  };
}
