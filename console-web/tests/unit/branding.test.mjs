// PRD-088 Console contract: strict branding DTOs, RBAC ownership, stale-response isolation,
// safe login/shell projection, and bounded PNG object URL lifecycle.
import assert from "node:assert/strict";
import test from "node:test";

import {
  createBrandingObjectUrl,
  getBranding,
  getBrandingLogo,
  getPublicBranding,
  getPublicBrandingLogo,
  normalizeBrandingPatch,
  normalizeBrandingResponse,
  normalizePublicBrandingResponse,
  pngFileToBase64,
  revokeBrandingObjectUrl,
  safeBrandingLogoUrl,
  updateBranding,
} from "../../src/lib/branding.js";
import {
  brandingGlobalOwner,
  brandingIdentitySnapshot,
  brandingPageOwner,
  createBrandingPageState,
} from "../../src/features/branding/brandingPageState.js";
import { renderBrandingSettings } from "../../src/features/branding/renderBrandingSettings.js";
import { renderLoginPage } from "../../src/features/auth/renderLoginPage.js";
import { renderConsoleShell } from "../../src/features/console-shell/renderConsoleShell.js";

const TENANT_A = "tenant_branding_a";
const USER_A = "usr_branding_admin_a";

function protectedBranding(overrides = {}) {
  return {
    branding_config_id: "brc_0123456789abcdef0123456789abcdef",
    product_name: "Acme AI Gateway",
    primary_color: "#176B87",
    secondary_color: "#F59E0B",
    login_title: "Acme AI 控制台",
    login_message: "安全、可追踪的企业模型网关",
    custom_domain: "console.acme.cn",
    custom_domain_status: "reserved",
    logo_present: true,
    logo_sha256: "a".repeat(64),
    logo_width: 64,
    logo_height: 64,
    revision: 3,
    status: "active",
    updated_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

function protectedEnvelope(overrides = {}) {
  return {
    branding: protectedBranding(overrides),
    request_id: "req_branding_1",
    trace_id: "trc_branding_1",
  };
}

function publicEnvelope(overrides = {}) {
  return {
    branding: {
      product_name: "Acme AI Gateway",
      primary_color: "#176B87",
      secondary_color: "#F59E0B",
      login_title: "Acme AI 控制台",
      login_message: "安全、可追踪的企业模型网关",
      logo_present: true,
      ...overrides,
    },
    request_id: "req_public_branding_1",
    trace_id: "trc_public_branding_1",
  };
}

function authContext({
  tenantId = TENANT_A,
  userId = USER_A,
  permissions = ["branding.read", "branding.manage"],
  revision = 17,
  fingerprint = "fp_branding_a",
  sessionId = "ses_branding_a",
} = {}) {
  return {
    user: { tenant_id: tenantId, user_id: userId, role: "admin", display_name: "Admin", email: "admin@acme.cn" },
    tenant: { tenant_id: tenantId, name: "Acme" },
    session: { session_id: sessionId, expires_at: "2026-08-18T02:00:00Z" },
    permission_codes: permissions,
    permission_revision: revision,
    authorization_fingerprint: fingerprint,
  };
}

function brandingShell({ includeBranding = true, requiredPermissions = ["branding.read"] } = {}) {
  return {
    state: "ok",
    activeNavigationItemId: "branding",
    navigation: {
      active_navigation_item_id: "branding",
      navigation_items: includeBranding ? [{
        id: "branding",
        label: "白标设置",
        group: "客户与渠道",
        target_section: "branding",
        description: "配置租户品牌",
        empty_state_hint: "读取品牌配置",
        required_permissions: requiredPermissions,
        visible: true,
        disabled: false,
        order: 120,
      }] : [],
    },
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", "X-Request-ID": payload.request_id ?? "req_branding" },
  });
}

test("strict protected and public DTOs reject unknown, internal, and oversized fields", () => {
  const normalized = normalizeBrandingResponse(protectedEnvelope());
  assert.equal(normalized.branding.product_name, "Acme AI Gateway");
  assert.equal(Object.isFrozen(normalized.branding), true);
  assert.ok(normalizePublicBrandingResponse(publicEnvelope()));

  assert.equal(normalizeBrandingResponse({
    ...protectedEnvelope(),
    branding: { ...protectedBranding(), tenant_id: TENANT_A },
  }), null);
  assert.equal(normalizePublicBrandingResponse(publicEnvelope({ logo_url: "/branding/public/logo?tenant_slug=acme" })), null);
  assert.equal(normalizePublicBrandingResponse(publicEnvelope({ revision: 3 })), null);
  assert.equal(normalizeBrandingResponse(protectedEnvelope({ product_name: "x".repeat(81) })), null);
  assert.equal(normalizeBrandingResponse(protectedEnvelope({ login_title: "x".repeat(121) })), null);
  assert.equal(normalizeBrandingResponse(protectedEnvelope({ login_message: "x".repeat(241) })), null);
  assert.equal(normalizeBrandingResponse(protectedEnvelope({ custom_domain_status: "active" })), null);
  assert.equal(normalizeBrandingResponse(protectedEnvelope({ status: "disabled" })), null);
});

test("branding PATCH accepts only bare bounded base64 and normalizes an empty domain to null", async () => {
  const exactLimit = Buffer.alloc(128 * 1024, 0x41).toString("base64");
  const normalized = normalizeBrandingPatch({
    product_name: "Acme",
    login_title: "Acme Console",
    login_message: "Enterprise AI Gateway",
    primary_color: "#176B87",
    secondary_color: "#F59E0B",
    custom_domain: "",
    logo_png_base64: exactLimit,
  });
  assert.equal(normalized.custom_domain, null);
  assert.equal(normalized.logo_png_base64, exactLimit);

  assert.equal(normalizeBrandingPatch({ primary_color: "#125e75", secondary_color: "#f59e0b" }).primary_color, "#125E75");
  assert.equal(normalizeBrandingPatch({ primary_color: "#125e75", secondary_color: "#f59e0b" }).secondary_color, "#F59E0B");

  for (const input of [
    {},
    { tenant_id: TENANT_A },
    { logo_png_base64: "data:image/png;base64,iVBORw==" },
    { logo_png_base64: Buffer.alloc((128 * 1024) + 1, 0x41).toString("base64") },
    { logo_png_base64: "not_base64" },
  ]) {
    assert.throws(() => normalizeBrandingPatch(input), (error) => error.code === "branding.request_invalid");
  }

  const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  assert.equal(await pngFileToBase64(new Blob([bytes], { type: "image/png" })), "iVBORw0KGgo=");
  await assert.rejects(
    pngFileToBase64(new Blob([Buffer.alloc((128 * 1024) + 1)], { type: "image/png" })),
    (error) => error.code === "branding.request_invalid",
  );
});

test("branding clients keep tenant authority server-side and preserve PATCH CAS", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return jsonResponse(protectedEnvelope({ logo_present: false, logo_sha256: null, logo_width: null, logo_height: null }));
  };

  await getBranding({ controlApiUrl: "http://control.local", token: "fixture-session-token" });
  await updateBranding({
    controlApiUrl: "http://control.local",
    token: "fixture-session-token",
    revision: 3,
    input: { product_name: "Acme", custom_domain: "" },
  });
  await getPublicBranding({ controlApiUrl: "http://control.local", tenantSlug: "Acme_Corp" }).catch(() => {});

  assert.equal(calls[0].url, "http://control.local/branding");
  assert.equal(calls[0].options.headers.Authorization, "Bearer fixture-session-token");
  assert.equal(calls[1].options.method, "PATCH");
  assert.equal(calls[1].options.headers["If-Match"], "3");
  assert.deepEqual(JSON.parse(calls[1].options.body), { product_name: "Acme", custom_domain: null });
  assert.equal(calls[2].url, "http://control.local/branding/public?tenant_slug=acme_corp");
  assert.equal(Object.hasOwn(calls[2].options.headers ?? {}, "Authorization"), false);
  assert.doesNotMatch(JSON.stringify(calls.map((call) => ({ url: call.url, body: call.options.body }))), /tenant_id|actor|logo_url/);
});

test("protected and public Logo reads validate PNG while keeping authorization boundaries", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), {
      headers: { "Content-Type": "image/png", "X-Request-ID": "req_logo_1" },
    });
  };

  const protectedLogo = await getBrandingLogo({ controlApiUrl: "http://control.local", token: "fixture-session-token" });
  const publicLogo = await getPublicBrandingLogo({ controlApiUrl: "http://control.local", tenantSlug: "acme" });
  assert.equal(protectedLogo.blob.type, "image/png");
  assert.equal(publicLogo.blob.size, 8);
  assert.equal(calls[0].options.headers.Authorization, "Bearer fixture-session-token");
  assert.equal(Object.hasOwn(calls[1].options.headers ?? {}, "Authorization"), false);
  assert.equal(calls[1].url, "http://control.local/branding/public/logo?tenant_slug=acme");

  globalThis.fetch = async () => new Response("<svg/>", {
    headers: { "Content-Type": "image/svg+xml", "X-Request-ID": "req_bad_logo" },
  });
  await assert.rejects(
    getBrandingLogo({ controlApiUrl: "http://control.local", token: "fixture-session-token" }),
    (error) => error.code === "branding.response_invalid" && error.requestId === "req_bad_logo",
  );
});

test("branding page owner requires live read permission and treats manage as an independent capability", () => {
  const manager = authContext();
  const managerOwner = brandingPageOwner(manager, brandingShell());
  assert.equal(managerOwner.canManage, true);

  const reader = authContext({ permissions: ["branding.read"], fingerprint: "fp_reader" });
  const readerOwner = brandingPageOwner(reader, brandingShell());
  assert.equal(readerOwner.canManage, false);

  const noRead = authContext({ permissions: ["branding.manage"], fingerprint: "fp_no_read" });
  assert.equal(brandingPageOwner(noRead, brandingShell()), null);
  assert.equal(brandingPageOwner(manager, brandingShell({ requiredPermissions: ["branding.manage"] })), null);

  const tenantMismatch = authContext();
  tenantMismatch.tenant.tenant_id = "tenant_other";
  assert.equal(brandingGlobalOwner(tenantMismatch), null);
  assert.equal(brandingIdentitySnapshot(tenantMismatch), "");
});

test("dynamic revocation clears CSS, DOM, and object URL before aborting the old read", (t) => {
  const events = [];
  let lateTicket = null;
  const urlApi = globalThis.URL;
  const previousRevoke = urlApi.revokeObjectURL;
  urlApi.revokeObjectURL = (url) => events.push(`revoke:${url}:${lateTicket?.controller.signal.aborted}`);
  t.after(() => { urlApi.revokeObjectURL = previousRevoke; });
  const documentApi = {
    documentElement: { style: {
      setProperty(name, value) { events.push(`set:${name}:${value}`); },
      removeProperty(name) { events.push(`remove:${name}:${lateTicket?.controller.signal.aborted}`); },
    } },
  };
  const state = createBrandingPageState({
    documentApi,
    clearDom() { events.push(`dom:${lateTicket?.controller.signal.aborted}`); },
  });
  const owner = brandingPageOwner(authContext(), brandingShell());
  assert.equal(state.enter(owner), true);
  const first = state.beginRead(owner);
  assert.equal(state.acceptRead(first, protectedEnvelope(), owner, "blob:tenant-a-logo"), true);
  state.finish(first);
  lateTicket = state.beginRead(owner);
  events.length = 0;

  const revokedOwner = brandingPageOwner(
    authContext({ permissions: [], revision: 18, fingerprint: "fp_revoked" }),
    brandingShell({ includeBranding: false }),
  );
  assert.equal(revokedOwner, null);
  assert.equal(state.enter(revokedOwner), false);
  assert.deepEqual(events, [
    "revoke:blob:tenant-a-logo:false",
    "remove:--branding-primary:false",
    "remove:--branding-secondary:false",
    "dom:false",
  ]);
  assert.equal(lateTicket.controller.signal.aborted, true);
  assert.equal(state.acceptRead(lateTicket, protectedEnvelope({ product_name: "Late tenant" }), owner), false);
  assert.equal(state.snapshot().branding, null);
  assert.equal(state.snapshot().logoUrl, "");
});

test("tenant switch rejects late responses and replaces the previous Logo URL exactly once", (t) => {
  const revoked = [];
  const urlApi = globalThis.URL;
  const previousRevoke = urlApi.revokeObjectURL;
  urlApi.revokeObjectURL = (url) => revoked.push(url);
  t.after(() => { urlApi.revokeObjectURL = previousRevoke; });
  const state = createBrandingPageState();
  const ownerA = brandingGlobalOwner(authContext());
  state.enter(ownerA);
  const first = state.beginRead(ownerA);
  state.acceptRead(first, protectedEnvelope(), ownerA, "blob:logo-a");
  state.finish(first);
  const lateA = state.beginRead(ownerA);

  const ownerB = brandingGlobalOwner(authContext({
    tenantId: "tenant_branding_b",
    userId: "usr_branding_admin_b",
    sessionId: "ses_branding_b",
    revision: 21,
    fingerprint: "fp_branding_b",
  }));
  assert.equal(state.enter(ownerB), true);
  assert.equal(lateA.controller.signal.aborted, true);
  assert.equal(state.acceptRead(lateA, protectedEnvelope({ product_name: "Late A" }), ownerA, "blob:late-a"), false);
  const readB = state.beginRead(ownerB);
  assert.equal(state.acceptRead(readB, protectedEnvelope({ product_name: "Tenant B" }), ownerB, "blob:logo-b"), true);
  assert.equal(state.snapshot().branding.product_name, "Tenant B");
  assert.equal(state.snapshot().logoUrl, "blob:logo-b");
  assert.deepEqual(revoked, ["blob:logo-a"]);
});

test("login, settings, and shell render escaped branding and never render remote Logo URLs", () => {
  const maliciousName = "Acme <script>alert(1)</script>";
  const loginContainer = { innerHTML: "" };
  renderLoginPage(loginContainer, {
    tenantSlug: "acme",
    branding: {
      product_name: maliciousName,
      login_title: "Welcome <img src=x onerror=alert(1)>",
      login_message: "Secure & traceable",
      logoPresent: true,
      logoUrl: "https://attacker.invalid/logo.png",
    },
  });
  assert.match(loginContainer.innerHTML, /Acme &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(loginContainer.innerHTML, /attacker\.invalid|<script>|<img/);
  assert.doesNotMatch(loginContainer.innerHTML, /class="brand-logo"/);

  renderLoginPage(loginContainer, {
    branding: {
      product_name: "Acme",
      login_title: "Acme Console",
      login_message: "Enterprise Gateway",
      logoPresent: true,
      logoUrl: "blob:acme-logo",
    },
  });
  assert.match(loginContainer.innerHTML, /src="blob:acme-logo"/);

  const auth = authContext();
  const shell = brandingShell();
  const shellHtml = renderConsoleShell({
    authContext: auth,
    consoleShellState: shell,
    contentHtml: "<section>Trusted content</section>",
    branding: { product_name: maliciousName, logoPresent: true, logoUrl: "blob:shell-logo" },
  });
  assert.match(shellHtml, /src="blob:shell-logo"/);
  assert.match(shellHtml, /Acme &lt;script&gt;/);
  assert.doesNotMatch(shellHtml, /<script>|fixture-session-token|tenant_id|logo_png_base64/i);

  const snapshot = { phase: "ready", branding: protectedBranding({ product_name: maliciousName }), logoUrl: "https://attacker.invalid/logo.png", mutationState: "idle" };
  const readerOwner = { ...brandingPageOwner(authContext({ permissions: ["branding.read"] }), shell), canManage: false };
  const readOnlyHtml = renderBrandingSettings(snapshot, readerOwner);
  assert.match(readOnlyHtml, /当前账号只读/);
  assert.doesNotMatch(readOnlyHtml, /class="primary-button"|attacker\.invalid|<script>/);
  const managerHtml = renderBrandingSettings({ ...snapshot, logoUrl: "blob:settings-logo" }, brandingPageOwner(auth, shell));
  assert.match(managerHtml, /class="primary-button"/);
  assert.match(managerHtml, /src="blob:settings-logo"/);
});

test("object URL helpers accept bounded PNG Blobs and revoke only local Blob URLs", () => {
  const events = [];
  const urlApi = {
    createObjectURL(blob) { events.push(`create:${blob.type}:${blob.size}`); return "blob:branding-logo"; },
    revokeObjectURL(url) { events.push(`revoke:${url}`); },
  };
  const url = createBrandingObjectUrl(new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], { type: "image/png" }), urlApi);
  assert.equal(url, "blob:branding-logo");
  assert.equal(safeBrandingLogoUrl(url), url);
  assert.equal(safeBrandingLogoUrl("data:image/png;base64,iVBORw=="), "");
  assert.equal(safeBrandingLogoUrl("https://attacker.invalid/logo.png"), "");
  revokeBrandingObjectUrl(url, urlApi);
  revokeBrandingObjectUrl("https://attacker.invalid/logo.png", urlApi);
  assert.deepEqual(events, ["create:image/png:4", "revoke:blob:branding-logo"]);

  assert.throws(
    () => createBrandingObjectUrl(new Blob(["svg"], { type: "image/svg+xml" }), urlApi),
    (error) => error.code === "branding.response_invalid",
  );
  assert.throws(
    () => createBrandingObjectUrl(new Blob([Buffer.alloc((128 * 1024) + 1)], { type: "image/png" }), urlApi),
    (error) => error.code === "branding.response_invalid",
  );
});
