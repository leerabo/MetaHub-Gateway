import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderChannelManagement } from "../../src/features/channels/renderChannelManagement.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import {
  approveChannelAttribution,
  commandChannelPartner,
  listChannelPartners,
  listChannelStatements,
  registerChannelLead,
  requestChannelAttribution,
} from "../../src/lib/channelManagement.js";
import { listPartnerWorkspaces, PartnerApiError } from "../../src/lib/partnerChannel.js";
import { PlatformApiError } from "../../src/lib/platform.js";

const CONTROL_URL = "http://control.local";
const CSRF = "C".repeat(43);
const STEP_UP = "G".repeat(43);
const IDEMPOTENCY = "channel-command-intent-0001";
const PARTNER_ID = `par_${"1".repeat(32)}`;
const ACCOUNT_ID = `gac_${"2".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"3".repeat(32)}`;
const TARGET_WORKSPACE_ID = `wsp_${"4".repeat(32)}`;
const MEMBERSHIP_ID = `chm_${"5".repeat(32)}`;
const RULE_ID = `ccr_${"6".repeat(32)}`;
const LEAD_ID = `cld_${"7".repeat(32)}`;
const ATTRIBUTION_ID = `wat_${"8".repeat(32)}`;
const CHANGE_ID = `atc_${"9".repeat(32)}`;
const STATEMENT_ID = `cst_${"a".repeat(32)}`;
const NOW = "2026-08-31T08:00:00.000Z";
const LATER = "2026-09-01T08:00:00.000Z";

test("PRD-139 clients keep six fixed routes, Cookie auth, CAS, and strict DTOs", async (t) => {
  installDocument(t);
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/platform/channel-partners" && (options.method ?? "GET") === "GET") {
      return jsonResponse(partnerList());
    }
    if (path === "/platform/channel-partners") return jsonResponse(partnerResponse(), 201);
    if (path === "/platform/channel-leads") return jsonResponse(leadResponse(), 201);
    if (path === "/platform/workspace-attributions") {
      return jsonResponse(attributionResponse(), 201);
    }
    if (path === `/platform/workspace-attributions/${ATTRIBUTION_ID}/approve`) {
      return jsonResponse(attributionResponse({ status: "active", approval_count: 1 }));
    }
    if (path === "/platform/channel-statements") return jsonResponse(statementList());
    throw new Error(`unexpected route ${path}`);
  });

  await listChannelPartners({ controlApiUrl: CONTROL_URL });
  await commandChannelPartner({
    controlApiUrl: CONTROL_URL,
    command: {
      operation: "create",
      owner_workspace_id: WORKSPACE_ID,
      owner_account_id: ACCOUNT_ID,
      partner_code: "north-channel",
      display_name: "North Channel",
      tier_code: "gold",
      region_scope: "apac",
      contract_sha256: "b".repeat(64),
      confirmed: true,
    },
    expectedRevision: 0,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await registerChannelLead({
    controlApiUrl: CONTROL_URL,
    command: {
      channel_partner_id: PARTNER_ID,
      expected_revision: 3,
      lead_code: "lead-001",
      referral_code: null,
      region_code: "apac",
      contact_email: "lead@example.test",
      contact_phone: null,
      source_sha256: "c".repeat(64),
      candidate_workspace_id: TARGET_WORKSPACE_ID,
      confirmed: true,
    },
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await requestChannelAttribution({
    controlApiUrl: CONTROL_URL,
    command: {
      channel_partner_id: PARTNER_ID,
      target_workspace_id: TARGET_WORKSPACE_ID,
      channel_lead_id: LEAD_ID,
      channel_commission_rule_snapshot_id: RULE_ID,
      reason_code: "channel.assignment",
      expected_attribution_revision: 0,
      starts_at: LATER,
      confirmed: true,
    },
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await approveChannelAttribution({
    controlApiUrl: CONTROL_URL,
    attributionId: ATTRIBUTION_ID,
    command: {
      expected_revision: 1,
      decision: "approved",
      reason_code: "owner.approved",
      confirmed: true,
    },
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await listChannelStatements({
    controlApiUrl: CONTROL_URL,
    channelPartnerId: PARTNER_ID,
    currency: "CNY",
  });

  assert.deepEqual(calls.map(({ url, options }) => [
    options.method ?? "GET", new URL(url).pathname,
  ]), [
    ["GET", "/platform/channel-partners"],
    ["POST", "/platform/channel-partners"],
    ["POST", "/platform/channel-leads"],
    ["POST", "/platform/workspace-attributions"],
    ["POST", `/platform/workspace-attributions/${ATTRIBUTION_ID}/approve`],
    ["GET", "/platform/channel-statements"],
  ]);
  calls.forEach(({ options }) => {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
  });
  const writes = calls.filter(({ options }) => options.method === "POST");
  assert.deepEqual(writes.map(({ options }) => options.headers["If-Match"]), [
    '"0"', '"3"', '"0"', '"1"',
  ]);
  writes.forEach(({ options }) => {
    assert.equal(options.headers["X-CSRF-Token"], CSRF);
    assert.equal(options.headers["X-Step-Up-Grant"], STEP_UP);
    assert.equal(options.headers["Idempotency-Key"], IDEMPOTENCY);
  });

  globalThis.fetch = async () => jsonResponse({ ...partnerList(), endpoint_url: "internal" });
  await assert.rejects(
    listChannelPartners({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError
      && error.code === "channel.response_invalid" && error.status === 502,
  );
  await assert.rejects(
    commandChannelPartner({
      controlApiUrl: CONTROL_URL,
      command: { operation: "update", channel_partner_id: "bad", expected_revision: 1,
        display_name: "Bad", tier_code: "gold", region_scope: "apac", status: "active",
        confirmed: true },
      expectedRevision: 1,
      stepUpGrant: STEP_UP,
      idempotencyKey: IDEMPOTENCY,
    }),
    (error) => error instanceof PlatformApiError
      && error.code === "channel.request_invalid" && error.status === 400,
  );
});

test("PRD-139 Partner entry uses only the Partner projection and rejects Platform facts", async (t) => {
  const calls = installFetch(t, () => jsonResponse(partnerWorkspaceList()));
  const result = await listPartnerWorkspaces({ controlApiUrl: CONTROL_URL });
  assert.equal(result.items[0].workspace_id, TARGET_WORKSPACE_ID);
  assert.equal(result.partner.commission_rules.length, 0);
  assert.equal(result.partner.pending_attributions.length, 0);
  assert.equal(new URL(calls[0].url).pathname, "/partner/workspaces");
  assert.equal(calls[0].options.headers.Authorization, undefined);

  globalThis.fetch = async () => jsonResponse(partnerWorkspaceList({
    partner: partner({ commission_rules: [rule()], pending_attributions: [] }),
  }));
  await assert.rejects(
    listPartnerWorkspaces({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PartnerApiError
      && error.code === "partner.response_invalid" && error.status === 502,
  );

  const [entry, client, workspaceEntry] = await Promise.all([
    readFile(new URL("../../src/features/partner/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/partnerChannel.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
  ]);
  assert.match(entry, /partnerChannel\.js/u);
  assert.doesNotMatch(entry, /platform\.js|workspaces\.js|customerSupport\.js|partnerPortal\.js/u);
  assert.doesNotMatch(client, /\/platform\/|\/partners\//u);
  assert.equal((client.match(/\/partner\/workspaces/gu) ?? []).length, 1);
  assert.equal(
    (workspaceEntry.match(/loadPartnerPortalState\(consoleShellState, refreshId\)/gu) ?? []).length,
    1,
    "the legacy loader definition may remain until PRD-147, but Workspace must not call it",
  );
  assert.doesNotMatch(workspaceEntry, /case "partner-portal":/u);
});

test("PRD-139 renderer gates directory, commands, approvals, and statements independently", () => {
  const state = channelState();
  const channel = renderChannelManagement(state, new Set([
    "platform.channel.read",
    "platform.channel.manage",
    "platform.channel.lead.register",
    "platform.channel.attribution.request",
    "platform.channel.attribution.approve",
    "platform.channel.statement.read",
  ]));
  assert.match(channel, /Partner directory/u);
  assert.match(channel, /data-platform-channel-partner-create-form/u);
  assert.match(channel, /data-platform-channel-lead-form/u);
  assert.match(channel, /data-platform-channel-attribution-form/u);
  assert.match(channel, /data-platform-channel-approval-form/u);
  assert.match(channel, /Channel statements/u);

  const billing = renderChannelManagement(state, new Set([
    "platform.channel.statement.read",
  ]));
  assert.match(billing, /Channel statements/u);
  assert.doesNotMatch(billing, /Partner directory|New partner|Register lead|Pending attribution/u);

  const enterprise = renderChannelManagement(state, new Set());
  assert.doesNotMatch(enterprise,
    /Partner directory|New partner|Register lead|Pending attribution|Channel statements/u);
});

test("PRD-139 approval controls fit the desktop decision column and stack on mobile", async () => {
  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  assert.match(css,
    /\.platform-channel-approvals th:last-child,[\s\S]*?\.platform-channel-approvals td:last-child\s*\{\s*width:\s*38%/u);
  assert.match(css,
    /@media \(max-width: 760px\)[\s\S]*?\.platform-channel-approvals td:last-child\s*\{\s*width:\s*100%/u);
  assert.match(css,
    /\.platform-channel-approvals form\s*\{[^}]*min-width:\s*0/u);
});

test("PRD-139 controller hashes sensitive intent, reuses retry key, aborts, and clears on revocation", async (t) => {
  installWindow(t);
  let serverPermissions = [
    "platform.channel.read", "platform.channel.manage", "platform.channel.statement.read",
  ];
  let attempts = 0;
  let keySequence = 0;
  const writes = [];
  const grants = [];
  let activeSignal = null;
  let holdReads = false;
  const api = {
    session: async () => ({ context: {
      permissions: [...serverPermissions],
      operator: { display_name: "Channel Ops", roles: ["platform_channel"] },
    } }),
    navigation: async () => ({ items: serverPermissions.includes("platform.channel.statement.read")
      ? [{ id: "channels", label: "Channels", href: "/platform.html#channels" }] : [] }),
    channelPartners: async ({ signal }) => {
      activeSignal = signal;
      if (holdReads) return new Promise((resolve, reject) => {
        signal.addEventListener("abort", () => reject(
          new DOMException("aborted", "AbortError"),
        ), { once: true });
      });
      return partnerList();
    },
    channelStatements: async () => statementList(),
    createIdentityStepUpGrant: async (input) => {
      grants.push(input);
      return { grant: STEP_UP };
    },
    commandChannelPartner: async (input) => {
      writes.push(input);
      attempts += 1;
      if (serverPermissions.length === 0) throw new PlatformApiError({
        status: 403, code: "platform.permission_denied", message: "revoked",
      });
      if (attempts === 1) throw new PlatformApiError({
        status: 503, code: "channel.unavailable", message: "retry",
      });
      return partnerResponse({ issued_referral_code: "rotated_referral_code_123456" });
    },
  };
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api,
    newIdempotencyKey: () => `channel-key-${String(++keySequence).padStart(16, "0")}`,
  });
  assert.equal(await controller.mount(), true);

  const secret = "referral-secret-value-001";
  const input = {
    partnerId: PARTNER_ID,
    expectedRevision: 3,
    referralCode: secret,
    expiresAt: null,
    usageLimit: 100,
    method: "totp",
    verificationCode: "123456",
    confirmed: true,
  };
  assert.equal(await controller.rotateChannelReferral(input), false);
  assert.equal(controller.snapshot().channelIntent.idempotencyKey, writes[0].idempotencyKey);
  assert.doesNotMatch(JSON.stringify(controller.snapshot().channelIntent), new RegExp(secret, "u"));
  assert.equal(await controller.rotateChannelReferral(input), true);
  assert.equal(writes[0].idempotencyKey, writes[1].idempotencyKey);
  assert.equal(grants.length, 2);
  assert.equal(controller.snapshot().channelIssuedReferral, "rotated_referral_code_123456");

  holdReads = true;
  const pending = controller.refreshChannels();
  await new Promise((resolve) => setImmediate(resolve));
  controller.selectView("overview");
  assert.equal(activeSignal.aborted, true);
  assert.equal(await pending, false);
  assert.equal(controller.snapshot().channels, null);

  holdReads = false;
  controller.selectView("channels");
  await new Promise((resolve) => setImmediate(resolve));
  serverPermissions = [];
  assert.equal(await controller.updateChannelPartner({
    partnerId: PARTNER_ID,
    expectedRevision: 3,
    displayName: "North Channel",
    tierCode: "gold",
    regionScope: "apac",
    status: "active",
    method: "totp",
    verificationCode: "654321",
    confirmed: true,
  }), false);
  assert.equal(controller.snapshot().view, "overview");
  assert.equal(controller.snapshot().channels, null);
  assert.equal(controller.snapshot().channelIntent, null);
  controller.destroy();
  assert.equal(controller.snapshot().session, null);
});

function channelState() {
  return {
    loading: false,
    channels: partnerList(),
    channelStatements: statementList(),
    channelFilters: { status: "", tierCode: "", regionScope: "" },
    channelStatementFilters: { partnerId: "", currency: "" },
    channelIssuedReferral: null,
    channelLatestLead: null,
    channelLatestAttribution: null,
  };
}

function partner(overrides = {}) {
  return {
    channel_partner_id: PARTNER_ID,
    owner_workspace_id: WORKSPACE_ID,
    partner_code: "north-channel",
    display_name: "North Channel",
    tier_code: "gold",
    region_scope: "apac",
    contract_sha256: "b".repeat(64),
    status: "active",
    revision: 3,
    memberships: [{
      channel_membership_id: MEMBERSHIP_ID,
      account_id: ACCOUNT_ID,
      role_code: "partner_owner",
      status: "active",
      revision: 1,
    }],
    referral: null,
    commission_rules: [rule()],
    pending_attributions: [pendingAttribution()],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function rule() {
  return {
    channel_commission_rule_snapshot_id: RULE_ID,
    currency: "CNY",
    commission_bps: 800,
    version: 1,
    effective_from: NOW,
    effective_until: null,
  };
}

function pendingAttribution() {
  return {
    workspace_attribution_id: ATTRIBUTION_ID,
    attribution_change_id: CHANGE_ID,
    target_workspace_id: TARGET_WORKSPACE_ID,
    starts_at: LATER,
    required_approvals: 1,
    approval_count: 0,
    approval_generation: 1,
    revision: 1,
  };
}

function partnerList(overrides = {}) {
  return {
    items: [partner()], total: 1, limit: 50, offset: 0,
    request_id: "req_prd139_partners", trace_id: "trc_prd139_partners", ...overrides,
  };
}

function partnerResponse(overrides = {}) {
  return {
    partner: partner(), issued_referral_code: null, idempotent_replay: false,
    request_id: "req_prd139_partner", trace_id: "trc_prd139_partner", ...overrides,
  };
}

function leadResponse() {
  return {
    lead: {
      channel_lead_id: LEAD_ID,
      channel_partner_id: PARTNER_ID,
      lead_code: "lead-001",
      region_code: "apac",
      stage: "registered",
      source_sha256: "c".repeat(64),
      candidate_workspace_id: TARGET_WORKSPACE_ID,
      revision: 1,
      created_at: NOW,
    },
    idempotent_replay: false,
    request_id: "req_prd139_lead",
    trace_id: "trc_prd139_lead",
  };
}

function attributionResponse(overrides = {}) {
  return {
    attribution: {
      workspace_attribution_id: ATTRIBUTION_ID,
      attribution_change_id: CHANGE_ID,
      channel_partner_id: PARTNER_ID,
      target_workspace_id: TARGET_WORKSPACE_ID,
      status: "pending_approval",
      starts_at: LATER,
      ends_at: null,
      required_approvals: 1,
      approval_count: 0,
      approval_generation: 1,
      revision: 1,
      approvals: [],
      ...overrides,
    },
    idempotent_replay: false,
    request_id: "req_prd139_attribution",
    trace_id: "trc_prd139_attribution",
  };
}

function statementList() {
  return {
    items: [{
      channel_statement_id: STATEMENT_ID,
      channel_partner_id: PARTNER_ID,
      period_start: NOW,
      period_end: LATER,
      currency: "CNY",
      total_amount_micros: 10_000_000,
      total_commission_micros: 800_000,
      line_count: 1,
      reconciliation_sha256: "d".repeat(64),
      status: "closed",
      close_revision: 1,
      closed_at: LATER,
    }],
    total: 1,
    limit: 50,
    offset: 0,
    request_id: "req_prd139_statements",
    trace_id: "trc_prd139_statements",
  };
}

function partnerWorkspaceList(overrides = {}) {
  return {
    partner: partner({ commission_rules: [], pending_attributions: [] }),
    role_code: "partner_owner",
    items: [{
      workspace_id: TARGET_WORKSPACE_ID,
      display_name: "Assigned Workspace",
      region_code: "apac",
      attribution_starts_at: LATER,
      operational_status: "active",
      statement_totals: { CNY: 800_000 },
    }],
    total: 1,
    limit: 50,
    offset: 0,
    request_id: "req_prd139_partner_workspaces",
    trace_id: "trc_prd139_partner_workspaces",
    ...overrides,
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: {
    "Cache-Control": "private, no-store",
    "Content-Type": "application/json",
  } });
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

function installWindow(t) {
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash: "#channels" },
    history: { replaceState() {} },
  };
  t.after(() => { globalThis.window = originalWindow; });
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}
