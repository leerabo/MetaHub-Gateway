import assert from "node:assert/strict";
import test from "node:test";
import { createWorkspaceCommercialAccountingController } from "../../src/features/workspace-commercial-accounting/workspaceCommercialAccountingController.js";
import { renderProviderPanel } from "../../src/features/providers/renderProviderPanel.js";
import { renderProviderCatalog } from "../../src/features/providers/renderProviderCatalog.js";
import { renderInvitationAcceptancePage } from "../../src/features/invitations/renderInvitationAcceptancePage.js";
import { previewInvitation } from "../../src/lib/invitations.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";

function commercialContext() {
  const expires = new Date(Date.now() + 60000).toISOString();
  const authorization = { permission_codes: [PERMISSIONS.WORKSPACE_COMMERCIAL_SUBSCRIPTION_READ],
    permission_revision: 2, membership_revision: 3, permission_fingerprint: "a".repeat(64) };
  return { authContext: authorization, workspaceIdentity: {
    account: { status: "active" }, current_session: { session_id: "global_test", is_current: true,
      revoke_generation: 1, expires_at: expires } },
    session: { context: { workspace_id: "wsp_" + "b".repeat(32), role: "owner",
      session_status: "active", expires_at: expires }, authorization },
    activeNavigationItemId: "workspace-subscription" };
}

test("subscription loads with real Workspace DTOs and no legacy identity fields", async () => {
  const setup = commercialContext();
  let reads = 0;
  const controller = createWorkspaceCommercialAccountingController({ api: {
    workspaceSession: async () => setup.session,
    workspaceSubscription: async () => { reads++; return { subscription: { subscription_status: "active" } }; },
  } });
  assert.equal(await controller.open(setup), true);
  assert.equal(reads, 1);
  assert.equal(controller.snapshot().phase, "ready");
  controller.close();
});

for (const drift of ["permission_fingerprint", "membership_revision", "permission_revision"]) {
  test(`subscription refuses ${drift} drift without leaving a permanent idle state`, async () => {
    const setup = commercialContext();
    setup.authContext = { ...setup.authContext, [drift]: drift.endsWith("fingerprint") ? "c".repeat(64) : 9 };
    const controller = createWorkspaceCommercialAccountingController({ api: {
      workspaceSession: async () => setup.session,
      workspaceSubscription: async () => assert.fail("unverified identity requested subscription"),
    } });
    assert.equal(await controller.open(setup), false);
    assert.equal(controller.snapshot().phase, "permission_revoked");
    controller.close();
  });
}

test("model services default to a list and closed custom configuration", () => {
  const html = renderProviderPanel({ state: "ok", providers: [], providerCatalog: { entries: [] } }, {
    permission_codes: [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE, PERMISSIONS.PROVIDER_CATALOG_READ],
  });
  assert.match(html, /添加模型服务/u);
  assert.match(html, /<details class="provider-custom-setup">/u);
  assert.doesNotMatch(html, /data-provider-catalog-create-form/u);
});

test("catalog hides all configurations until a manufacturer is selected", () => {
  const entries = ["deepseek", "moonshot"].map((entry_code) => ({ entry_code,
    display_name: entry_code, status: "active", instantiation_mode: "official_cloud" }));
  const auth = { permission_codes: [PERMISSIONS.PROVIDER_CATALOG_READ, PERMISSIONS.PROVIDER_WRITE] };
  const html = renderProviderCatalog({ entries }, auth);
  assert.equal((html.match(/data-provider-catalog-entry="[^"]+" hidden/gu) ?? []).length, 2);
  const selected = renderProviderCatalog({ entries, selectedEntryCode: "moonshot" }, auth);
  assert.match(selected, /data-provider-catalog-entry="moonshot">/u);
  assert.match(selected, /data-provider-catalog-entry="deepseek" hidden/u);
});

test("invitation preview omits existing credentials and exposes only bound summary", async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const summary = { email: "employee@example.test", workspace_name: "Trial", role: "developer" };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "http://trial.test/invitations/preview");
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
    assert.equal(options.referrerPolicy, "no-referrer");
    assert.equal(options.headers.Authorization, undefined);
    assert.deepEqual(JSON.parse(options.body), { token: "A".repeat(43) });
    return { ok: true, json: async () => summary };
  };
  const invitation = await previewInvitation({ controlApiUrl: "http://trial.test", token: "A".repeat(43) });
  const html = renderInvitationAcceptancePage({ invitation });
  assert.match(html, /employee@example.test/u);
  assert.match(html, /后台成员邀请|加入并登录/u);
  assert.doesNotMatch(html, /name="email"|AAAAAAAAAAAA/u);
});
