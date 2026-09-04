// PRD-087 客户端合约测试：覆盖统一错误、精确响应键、租户边界和附件安全限制。
import assert from "node:assert/strict";
import test from "node:test";

import { createI18n } from "../../src/i18n/messages.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import {
  configurePartner,
  downloadPartnerCommissionCsv,
  downloadPartnerDeliveryPackage,
  normalizePartnerCommissionResponse,
  normalizePartnerCustomersResponse,
  normalizePartnerProfileResponse,
  normalizePartnerProjectsResponse,
  triggerPartnerAttachmentDownload,
  updatePartnerBranding,
} from "../../src/lib/partnerPortal.js";
import {
  createPartnerPortalPageState,
  partnerPortalPageOwner,
} from "../../src/features/partner-portal/partnerPortalPageState.js";
import { renderPartnerPortal } from "../../src/features/partner-portal/renderPartnerPortal.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";

const TENANT_ID = "tenant_partner_a";
const PARTNER_ID = `par_${"a".repeat(32)}`;
const CUSTOMER_ID = `pcu_${"b".repeat(32)}`;
const PROJECT_ID = `prj_${"c".repeat(32)}`;
const READ = PERMISSIONS.PARTNER_PORTAL_READ;
const ALL_PERMISSIONS = [
  READ,
  PERMISSIONS.PARTNER_PORTAL_CONFIGURE,
  PERMISSIONS.PARTNER_CUSTOMER_MANAGE,
  PERMISSIONS.PARTNER_BRANDING_MANAGE,
  PERMISSIONS.PARTNER_COMMISSION_EXPORT,
  PERMISSIONS.PARTNER_DELIVERY_DOWNLOAD,
];

function context(permissions = ALL_PERMISSIONS, tenantId = TENANT_ID, fingerprint = "partner_fp_a") {
  return {
    permission_codes: permissions,
    permission_revision: 22,
    authorization_fingerprint: fingerprint,
    user: {
      user_id: "usr_partner_admin", tenant_id: tenantId, role: "admin",
      permission_codes: permissions, permission_revision: 22, authorization_fingerprint: fingerprint,
    },
    tenant: { tenant_id: tenantId },
    session: { session_id: "ses_partner_a", session_revision: 1 },
  };
}

function shell(auth = context()) {
  return {
    state: "ok",
    activeNavigationItemId: "partner-portal",
    navigation: {
      active_navigation_item_id: "partner-portal",
      permission_codes: auth.permission_codes,
      permission_revision: 22,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [{
        id: "partner-portal", target_section: "partner-portal",
        required_permissions: [READ], visible: true, disabled: false,
      }],
    },
  };
}

function partner() {
  return {
    partner_id: PARTNER_ID,
    partner_code: "acme.channel",
    display_name: "Acme Channel",
    portal_name: "Acme AI Operations",
    brand_primary_color: "#176B5B",
    support_text: "Enterprise support desk",
    status: "active",
    default_commission_rate_bps: 1000,
    revision: 2,
    created_at: "2026-08-17T00:00:00Z",
    updated_at: "2026-08-17T01:00:00Z",
  };
}

function envelope(values = {}) {
  return { ...values, request_id: "req_partner_1", trace_id: "trc_partner_1" };
}

function windowDto() {
  return { start_date: "2026-07-18", end_date: "2026-08-16", timezone: "UTC" };
}

function usage() {
  return {
    usage_scope: "project_history", start_date: "2026-07-18", end_date: "2026-08-16",
    request_count: 12, success_count: 10, failed_count: 2,
    input_tokens: 70, output_tokens: 30, total_tokens: 100, cache_tokens: 8,
    cost_cny_micros: 2500000, cost_usd_micros: 125000, estimated_records: 1,
  };
}

function customer() {
  return {
    partner_customer_id: CUSTOMER_ID,
    project_id: PROJECT_ID,
    customer_code: "north.division",
    display_name: "North Division",
    status: "active",
    commission_rate_bps: 1000,
    revision: 1,
    linked_from_ledger_sequence: 100,
    commission_cutoff_sequence: null,
    created_at: "2026-08-17T00:00:00Z",
    updated_at: "2026-08-17T00:00:00Z",
    disabled_at: null,
    usage: usage(),
    commission_cursor: {
      status: "pending", last_scanned_ledger_sequence: 110,
      source_high_water_sequence: 110, freshness_status: "current",
    },
  };
}

function customersPayload() {
  return envelope({ window: windowDto(), items: [customer()], total: 1, limit: 50, offset: 0 });
}

function commissionPayload() {
  return envelope({
    window: windowDto(),
    items: [{
      commission_record_id: "com_partner_1",
      partner_customer_id: CUSTOMER_ID,
      customer_code: "north.division",
      customer_name: "North Division",
      currency: "CNY",
      amount_micros: 2500000,
      commission_rate_bps: 1000,
      commission_amount_micros: 250000,
      input_tokens: 70,
      output_tokens: 30,
      total_tokens: 100,
      cache_tokens: 8,
      estimated: false,
      status: "accrued",
      settlement_status: "not_applicable",
      source_request_id: "req_source_1",
      source_trace_id: "trc_source_1",
      source_created_at: "2026-08-17T00:00:00Z",
    }],
    totals: [
      { currency: "CNY", request_count: 1, total_tokens: 100, amount_micros: 2500000, commission_amount_micros: 250000, estimated_records: 0 },
      { currency: "USD", request_count: 2, total_tokens: 200, amount_micros: 500000, commission_amount_micros: 50000, estimated_records: 1 },
    ],
    cursor: { materialized_through_sequence: 110, source_high_water_sequence: 110, freshness_status: "current" },
    total: 1, limit: 50, offset: 0,
  });
}

function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store, private", ...headers },
  });
}

test("PAGE-119 owner requires exact live read permission and backend navigation mirror", () => {
  const auth = context();
  const owner = partnerPortalPageOwner(auth, shell(auth));
  assert.ok(owner);
  assert.equal(owner.canConfigure, true);
  assert.equal(owner.canExportCommission, true);

  const roleOnly = context([]);
  assert.equal(partnerPortalPageOwner(roleOnly, shell(roleOnly)), null);
  const wrongTarget = shell(auth);
  wrongTarget.navigation.navigation_items[0].target_section = "overview";
  assert.equal(partnerPortalPageOwner(auth, wrongTarget), null);
  const drifted = shell(auth);
  drifted.navigation.permission_revision = 21;
  assert.equal(partnerPortalPageOwner(auth, drifted), null);
});

test("strict normalizers accept exact DTOs and reject extra fields, owner drift, and arithmetic drift", () => {
  assert.ok(normalizePartnerProfileResponse(envelope({ partner: partner() })));
  assert.ok(normalizePartnerProjectsResponse(envelope({
    items: [{ project_id: PROJECT_ID, project_code: "north.project", name: "North Project" }], total: 1,
  })));
  assert.ok(normalizePartnerCustomersResponse(customersPayload()));
  assert.ok(normalizePartnerCommissionResponse(commissionPayload()));

  assert.equal(normalizePartnerProfileResponse(envelope({ partner: { ...partner(), tenant_id: TENANT_ID } })), null);
  const badUsage = customersPayload();
  badUsage.items[0].usage.failed_count = 3;
  assert.equal(normalizePartnerCustomersResponse(badUsage), null);
  const wrongScope = customersPayload();
  wrongScope.items[0].usage.usage_scope = "post_link";
  assert.equal(normalizePartnerCustomersResponse(wrongScope), null);
  const activeComplete = customersPayload();
  activeComplete.items[0].commission_cursor.status = "complete";
  assert.equal(normalizePartnerCustomersResponse(activeComplete), null);
  const disabledComplete = customersPayload();
  disabledComplete.items[0].status = "disabled";
  disabledComplete.items[0].commission_cutoff_sequence = 110;
  disabledComplete.items[0].disabled_at = "2026-08-17T02:00:00Z";
  disabledComplete.items[0].commission_cursor.status = "complete";
  assert.ok(normalizePartnerCustomersResponse(disabledComplete));
  const mixedCurrency = commissionPayload();
  mixedCurrency.totals[1].currency = "CNY";
  assert.equal(normalizePartnerCommissionResponse(mixedCurrency), null);
  const leaked = commissionPayload();
  leaked.items[0].prompt = "must not survive";
  assert.equal(normalizePartnerCommissionResponse(leaked), null);
});

test("profile mutations send no tenant facts and freeze idempotency/CAS headers", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(envelope({ partner: partner() }), options.method === "POST" ? 201 : 200);
  };
  await configurePartner({
    controlApiUrl: "http://control.local", token: "fixture-session-token",
    idempotencyKey: "partner-1234567890abcdef",
    input: {
      partner_code: "acme.channel", display_name: "Acme Channel", portal_name: "Acme AI Operations",
      brand_primary_color: "#176b5b", support_text: "Enterprise support desk",
    },
  });
  await updatePartnerBranding({
    controlApiUrl: "http://control.local", token: "fixture-session-token", revision: 2,
    input: { portal_name: "Acme AI Operations", brand_primary_color: "#176B5B", support_text: "Enterprise support desk" },
  });
  const createBody = JSON.parse(calls[0].options.body);
  assert.equal(calls[0].url, "http://control.local/partners");
  assert.equal(calls[0].options.headers["Idempotency-Key"], "partner-1234567890abcdef");
  assert.equal(calls[1].options.headers["If-Match"], "2");
  assert.equal(createBody.brand_primary_color, "#176B5B");
  assert.doesNotMatch(JSON.stringify(createBody), /tenant_id|request_id|actor|default_commission_rate_bps/);
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.redirect, "error");
});

test("CSV and delivery attachments validate exact headers, byte limits, BOM, names, and immediate revoke", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const csv = new Blob([new Uint8Array([0xef, 0xbb, 0xbf]), "currency,amount\r\nCNY,1\r\n"], { type: "text/csv; charset=utf-8" });
  globalThis.fetch = async (url, options) => {
    const delivery = String(url).endsWith("delivery-package");
    const blob = delivery ? new Blob(["{}"], { type: "application/json; charset=utf-8" }) : csv;
    return new Response(blob, {
      headers: {
        "Content-Type": delivery ? "application/json; charset=utf-8" : "text/csv; charset=utf-8",
        "Content-Disposition": delivery
          ? 'attachment; filename="metahub-partner-delivery-acme.channel-2026-08-17.json"'
          : 'attachment; filename="partner-commission-acme.channel-2026-08-17.csv"',
        "Cache-Control": "no-store, private",
        "X-Request-ID": "req_attachment_1",
        "X-Trace-ID": "trc_attachment_1",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": String(blob.size),
      },
    });
  };
  const csvResult = await downloadPartnerCommissionCsv({
    controlApiUrl: "http://control.local", token: "fixture-session-token", partnerCode: "acme.channel",
    filters: { start_date: "2026-08-01", end_date: "2026-08-16" },
  });
  const deliveryResult = await downloadPartnerDeliveryPackage({
    controlApiUrl: "http://control.local", token: "fixture-session-token", partnerCode: "acme.channel",
  });
  assert.equal(csvResult.filename, "partner-commission-acme.channel-2026-08-17.csv");
  assert.equal(deliveryResult.filename, "metahub-partner-delivery-acme.channel-2026-08-17.json");
  const events = [];
  triggerPartnerAttachmentDownload(csvResult, {
    URL: {
      createObjectURL(blob) { events.push(`create:${blob.size}`); return "blob:partner"; },
      revokeObjectURL(url) { events.push(`revoke:${url}`); },
    },
    document: {
      body: { append() { events.push("append"); } },
      createElement() { return { click() { events.push("click"); }, remove() { events.push("remove"); } }; },
    },
  });
  assert.deepEqual(events.slice(-4), ["append", "click", "remove", "revoke:blob:partner"]);

  globalThis.fetch = async () => new Response(csv, { headers: {
    "Content-Type": "text/csv; charset=utf-8",
    "Content-Disposition": 'attachment; filename="../../private.csv"',
    "Cache-Control": "no-store, private", "X-Request-ID": "req_x", "X-Trace-ID": "trc_x",
    "X-Content-Type-Options": "nosniff",
  } });
  await assert.rejects(
    downloadPartnerCommissionCsv({ controlApiUrl: "http://control.local", token: "token", partnerCode: "acme.channel" }),
    (error) => error.code === "partner.attachment_invalid" && error.status === 502,
  );
});

test("state clears DOM and branding before abort, and drops late 200/401/403/503", () => {
  let observedSignal;
  const events = [];
  const state = createPartnerPortalPageState({
    clearBranding() { events.push(`branding:${observedSignal?.aborted ?? "none"}`); },
    clearDom() { events.push(`dom:${observedSignal?.aborted ?? "none"}`); },
  });
  const authA = context();
  const ownerA = partnerPortalPageOwner(authA, shell(authA));
  assert.equal(state.enter(ownerA), true);
  const late = state.beginProfile(ownerA, 1);
  observedSignal = late.controller.signal;

  const authB = context(ALL_PERMISSIONS, "tenant_partner_b", "partner_fp_b");
  const ownerB = partnerPortalPageOwner(authB, shell(authB));
  assert.equal(state.enter(ownerB), true);
  assert.deepEqual(events.slice(-2), ["branding:false", "dom:false"]);
  assert.equal(late.controller.signal.aborted, true);
  assert.equal(state.acceptProfile(late, envelope({ partner: partner() }), ownerA), false);
  assert.equal(state.rejectProfile(late, { status: 401, code: "auth.token_invalid" }, ownerA), false);
  assert.equal(state.rejectProfile(late, { status: 403, code: "auth.permission_denied" }, ownerA), false);
  assert.equal(state.rejectProfile(late, { status: 503, code: "partner.unavailable" }, ownerA), false);
});

test("renderer keeps setup RBAC, historical usage, post-link commission, currency separation, and safe errors", () => {
  const auth = context();
  const nav = shell(auth);
  const owner = partnerPortalPageOwner(auth, nav);
  const state = createPartnerPortalPageState();
  state.enter(owner);
  const profileTicket = state.beginProfile(owner, 1);
  state.acceptProfile(profileTicket, envelope({ partner: partner() }), owner);
  state.finish(profileTicket);
  const customersTicket = state.beginCustomers(owner);
  state.acceptCustomers(customersTicket, normalizePartnerCustomersResponse(customersPayload()), owner);
  state.finish(customersTicket);
  const commissionTicket = state.beginCommission(owner);
  state.acceptCommission(commissionTicket, normalizePartnerCommissionResponse(commissionPayload()), owner);
  state.finish(commissionTicket);
  const html = renderPartnerPortal(state.snapshot(), auth, nav, createI18n("zh-CN"));
  assert.match(html, /Acme AI Operations/);
  assert.match(html, /Project 建立映射前的历史/);
  assert.match(html, /生效后分润/);
  assert.match(html, /CNY/);
  assert.match(html, /USD/);
  assert.match(html, /type="color"/);
  assert.match(html, /data-partner-commission-export/);
  assert.doesNotMatch(html, /fixture-session-token|provider.?key|authorization|password|secret|prompt|response/i);
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: auth, consoleShellState: nav, partnerPortalState: state.snapshot(), i18n: createI18n("en-US"),
  });
  assert.doesNotMatch(container.innerHTML, /class="panel partner-portal-panel"/);
  assert.match(container.innerHTML, /data-active-navigation="partner-portal"/);

  const readOnlyAuth = context([READ], TENANT_ID, "partner_readonly");
  const readOnlyOwner = partnerPortalPageOwner(readOnlyAuth, shell(readOnlyAuth));
  const emptyState = createPartnerPortalPageState();
  emptyState.enter(readOnlyOwner);
  const ticket = emptyState.beginProfile(readOnlyOwner, 1);
  emptyState.rejectProfile(ticket, { status: 404, code: "partner.not_configured" }, readOnlyOwner);
  const emptyHtml = renderPartnerPortal(emptyState.snapshot(), readOnlyAuth, shell(readOnlyAuth), createI18n("en-US"));
  assert.match(emptyHtml, /Partner portal not configured/);
  assert.doesNotMatch(emptyHtml, /data-partner-setup-form/);
});
