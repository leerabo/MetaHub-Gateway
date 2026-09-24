import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createDeploymentBrandingController } from "../../src/features/brand-domains/deploymentBrandingController.js";
import { createPlatformBrandingController } from "../../src/features/brand-domains/platformBrandingController.js";
import {
  renderIdentityLoginPage,
  setIdentityPublicBranding,
} from "../../src/features/identity/renderIdentityPage.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  getChannelBranding,
  getDeploymentBranding,
  getPlatformBranding,
  getPublicBranding,
  pngFileToBase64,
  updateChannelBranding,
  updateDeploymentBranding,
  updatePlatformBranding,
} from "../../src/lib/brandDomains.js";
import { PlatformApiError } from "../../src/lib/platform.js";

const CONTROL_URL = "http://control.local";
const PLATFORM_CSRF = "P".repeat(43);
const WORKSPACE_CSRF = "W".repeat(43);
const STEP_UP = "G".repeat(43);
const IDEMPOTENCY = "brand-command-intent-0001";
const PLATFORM_ID = `pbr_${"1".repeat(32)}`;
const CHANNEL_ID = `cbr_${"2".repeat(32)}`;
const DEPLOYMENT_BRAND_ID = `dbr_${"3".repeat(32)}`;
const PARTNER_ID = `par_${"4".repeat(32)}`;
const PARTNER_ID_B = `par_${"5".repeat(32)}`;
const DEPLOYMENT_ID = `edp_${"6".repeat(32)}`;
const DEPLOYMENT_ID_B = `edp_${"7".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"8".repeat(32)}`;
const ASSET_ID = `bas_${"9".repeat(32)}`;
const DOMAIN_ID = `vdo_${"a".repeat(32)}`;
const NOW = "2026-09-01T04:00:00.000Z";

test("PRD-142 clients use four fixed Cookie routes and exact write boundaries", async (t) => {
  installDocument(t);
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    const method = options.method ?? "GET";
    if (path === "/platform/branding") {
      return brandResponse(platformBrand(), method === "PATCH");
    }
    if (path === `/platform/channel-partners/${PARTNER_ID}/branding`) {
      return brandResponse(channelBrand(), method === "PATCH");
    }
    if (path === `/deployments/${DEPLOYMENT_ID}/branding`) {
      return brandResponse(deploymentBrand(), method === "PATCH");
    }
    if (path === "/branding/public") return publicResponse(publicBrand());
    throw new Error(`unexpected route ${path}`);
  });

  await getPlatformBranding({ controlApiUrl: CONTROL_URL });
  await updatePlatformBranding(writeInput({ command: updateCommand() }));
  await getChannelBranding({ controlApiUrl: CONTROL_URL, partnerId: PARTNER_ID });
  await updateChannelBranding(writeInput({ partnerId: PARTNER_ID, command: { action: "release" } }));
  await getDeploymentBranding({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID });
  await updateDeploymentBranding(writeInput({
    deploymentId: DEPLOYMENT_ID,
    command: { action: "publish" },
  }));
  await getPublicBranding({ controlApiUrl: CONTROL_URL });

  assert.deepEqual(calls.map(({ url, options }) => [
    options.method ?? "GET", new URL(url).pathname,
  ]), [
    ["GET", "/platform/branding"],
    ["PATCH", "/platform/branding"],
    ["GET", `/platform/channel-partners/${PARTNER_ID}/branding`],
    ["PATCH", `/platform/channel-partners/${PARTNER_ID}/branding`],
    ["GET", `/deployments/${DEPLOYMENT_ID}/branding`],
    ["PATCH", `/deployments/${DEPLOYMENT_ID}/branding`],
    ["GET", "/branding/public"],
  ]);
  for (const { url, options } of calls) {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
    assert.equal(new URL(url).search, "");
  }
  const writes = calls.filter(({ options }) => options.method === "PATCH");
  assert.deepEqual(writes.map(({ options }) => options.headers["X-CSRF-Token"]), [
    PLATFORM_CSRF, PLATFORM_CSRF, WORKSPACE_CSRF,
  ]);
  for (const { options } of writes) {
    assert.equal(options.headers["If-Match"], '"2"');
    assert.equal(options.headers["Idempotency-Key"], IDEMPOTENCY);
    assert.equal(options.headers["X-Step-Up-Grant"], STEP_UP);
    assert.equal(typeof options.body, "string");
  }
  const publicCall = calls.at(-1);
  assert.equal(publicCall.options.body, undefined);
  assert.deepEqual(Object.keys(publicCall.options.headers), ["Accept"]);
});

test("strict brand responses reject extensions and public owner hints", async (t) => {
  installDocument(t);
  installFetch(t, () => brandResponse({ ...platformBrand(), tenant_id: "forbidden" }));
  await assert.rejects(
    getPlatformBranding({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError
      && error.status === 502 && error.code === "branding.response_invalid",
  );

  globalThis.fetch = async () => publicResponse({ ...publicBrand(), owner_kind: "channel" });
  await assert.rejects(
    getPublicBranding({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof AuthApiError
      && error.status === 502 && error.code === "branding.response_invalid",
  );
});

test("PNG conversion accepts only bounded image/png and returns bare base64", async () => {
  const png = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const encoded = await pngFileToBase64({
    type: "image/png",
    size: png.byteLength,
    async arrayBuffer() { return png.buffer; },
  });
  assert.equal(encoded, Buffer.from(png).toString("base64"));
  await assert.rejects(
    pngFileToBase64({ type: "image/svg+xml", size: 3, async arrayBuffer() { return new ArrayBuffer(3); } }),
    (error) => error instanceof AuthApiError && error.code === "branding.request_invalid",
  );
  await assert.rejects(
    pngFileToBase64({ type: "image/png", size: 512 * 1024 + 1, async arrayBuffer() { return new ArrayBuffer(1); } }),
    (error) => error instanceof AuthApiError && error.code === "branding.request_invalid",
  );
});

test("Platform and Channel controllers keep owner lanes isolated and scope Channel reads", async () => {
  const requestedPartners = [];
  const updateKeys = [];
  let updateAttempts = 0;
  const controller = createPlatformBrandingController({
    api: {
      async platformBrand() { return envelope(platformBrand()); },
      async channelBrand({ partnerId }) {
        requestedPartners.push(partnerId);
        return envelope(channelBrand({ channel_brand_id: partnerId === PARTNER_ID ? CHANNEL_ID : `cbr_${"b".repeat(32)}` }));
      },
      async createIdentityStepUpGrant() { return { grant: STEP_UP }; },
      async updatePlatformBrand({ idempotencyKey }) {
        updateKeys.push(idempotencyKey);
        updateAttempts += 1;
        if (updateAttempts === 1) throw new PlatformApiError({
          status: 503, code: "branding.dependency_unavailable", requestId: "req_retry",
        });
        return envelope(platformBrand({ revision: 3 }));
      },
      async getWebAuthnAssertion() { throw new Error("not used"); },
      async beginIdentityStepUp() { throw new Error("not used"); },
    },
    newIdempotencyKey: (() => { let value = 0; return () => `brand-intent-${++value}-00000000`; })(),
  });
  const context = platformContext();
  assert.equal(controller.enter(context, [{ id: "branding" }, { id: "channels" }]), true);
  await controller.loadPlatform();
  await controller.selectChannel(PARTNER_ID);
  assert.equal(controller.snapshot().platform.branding.platform_brand_id, PLATFORM_ID);
  assert.equal(controller.snapshot().channel.branding.channel_brand_id, CHANNEL_ID);
  await controller.selectChannel(PARTNER_ID_B);
  assert.deepEqual(requestedPartners, [PARTNER_ID, PARTNER_ID_B]);
  assert.equal(controller.snapshot().platform.branding.platform_brand_id, PLATFORM_ID);

  const input = { command: updateCommand(), method: "totp", verificationCode: "654321", confirmed: true };
  await controller.mutatePlatform(input);
  await controller.mutatePlatform(input);
  assert.equal(updateKeys[0], updateKeys[1], "same failed intent must reuse its idempotency key");
  assert.deepEqual(Object.keys(controller.snapshot().platform).sort(), [
    "branding", "busy", "error", "feedback", "phase",
  ]);
  controller.clear();
  assert.equal(controller.snapshot().platform.branding, null);
  assert.equal(controller.snapshot().channel.partnerId, "");
});

test("Deployment controller loads only a visible Deployment and never owns desired state", async () => {
  const calls = [];
  const updates = [];
  const controller = createDeploymentBrandingController({
    api: {
      async deploymentBrand({ deploymentId }) {
        calls.push(deploymentId);
        return envelope(deploymentBrand());
      },
      async updateDeploymentBrand(input) {
        updates.push(input);
        return envelope(deploymentBrand({ revision: 3, desired_generation: 2 }));
      },
      async createIdentityStepUpGrant(input) {
        assert.equal(input.workspaceId, WORKSPACE_ID);
        return { grant: STEP_UP };
      },
      async getWebAuthnAssertion() { throw new Error("not used"); },
      async beginIdentityStepUp() { throw new Error("not used"); },
    },
    newIdempotencyKey: () => "deployment-brand-intent-0001",
  });
  const owner = {
    readIdentity: "edge-read-owner",
    navigationOwner: "private-gateways",
    brandingReadIdentity: "deployment-brand-read",
    brandingManageIdentity: "deployment-brand-manage",
    canReadBranding: true,
    canManageBranding: true,
  };
  controller.enter(owner, [{ deployment_id: DEPLOYMENT_ID, workspace_id: WORKSPACE_ID }]);
  assert.equal(await controller.select(DEPLOYMENT_ID_B), false);
  assert.deepEqual(calls, []);
  assert.equal(await controller.select(DEPLOYMENT_ID), true);
  assert.deepEqual(calls, [DEPLOYMENT_ID]);
  assert.equal(await controller.mutate({
    command: { action: "publish" }, method: "totp", verificationCode: "123456", confirmed: true,
  }), true);
  assert.equal(updates[0].deploymentId, DEPLOYMENT_ID);
  assert.equal(Object.hasOwn(controller.snapshot(), "desiredState"), false);
  controller.clear();
  assert.equal(controller.snapshot().selectedDeploymentId, "");
  assert.equal(controller.snapshot().branding, null);
});

test("identity public brand is safe, ephemeral, and independent of identity startup", async () => {
  const styles = new Map();
  const favicon = { href: "", setAttribute(name, value) { if (name === "href") this.href = value; } };
  const app = { aria: "", setAttribute(name, value) { if (name === "aria-label") this.aria = value; } };
  const documentObject = {
    title: "",
    documentElement: { style: { setProperty(name, value) { styles.set(name, value); } } },
    querySelector(selector) {
      if (selector === "#identity-app") return app;
      if (selector === "link[rel='icon']") return favicon;
      return null;
    },
  };
  setIdentityPublicBranding(publicBrand({ product_name: "North <Cloud>" }), documentObject);
  const container = { innerHTML: "" };
  renderIdentityLoginPage(container);
  assert.match(container.innerHTML, /North &lt;Cloud&gt;/u);
  assert.match(container.innerHTML, /Host-scoped access/u);
  assert.match(container.innerHTML, new RegExp(`/branding/public/assets/${ASSET_ID}`));
  assert.equal(styles.get("--identity-accent"), "#176B4D");
  assert.equal(favicon.href, `/branding/public/assets/${ASSET_ID}`);
  assert.equal(app.aria, "North <Cloud> 账号安全");

  setIdentityPublicBranding({ ...publicBrand(), logo_path: "https://attacker.invalid/logo.png" }, documentObject);
  renderIdentityLoginPage(container);
  assert.doesNotMatch(container.innerHTML, /attacker\.invalid/u);

  const [identityMain, identityRenderer] = await Promise.all([
    readFile(new URL("../../src/features/identity/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/identity/renderIdentityPage.js", import.meta.url), "utf8"),
  ]);
  assert.match(identityMain, /void getPublicBranding\(\{ controlApiUrl \}\)/u);
  assert.match(identityMain, /void controller\.mount\(/u);
  assert.doesNotMatch(identityMain, /publicBrandingReady/u);
  assert.doesNotMatch(`${identityMain}\n${identityRenderer}`, /localStorage|sessionStorage|tenant_slug/u);
  setIdentityPublicBranding(null, documentObject);
});

test("old Workspace branding runtime, tenant query, and settings CSS are absent", async () => {
  const [entry, renderer, shell, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/console-shell/renderConsoleShell.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(entry, /features\/branding|lib\/branding|getBrandingLogo|updateBranding\(|brandingPageState|tenant_slug|publicBrandingTenantSlug/u);
  assert.doesNotMatch(renderer, /case "branding"|renderBrandingSettings|brandingState/u);
  assert.doesNotMatch(shell, /lib\/branding|blob:/u);
  assert.doesNotMatch(css, /branding-settings|branding-logo-preview/u);
  assert.match(entry, /getPublicBranding as getHostPublicBranding/u);
});

test("pending Deployment branding is visible but cannot issue a brand request", async () => {
  const renderer = await readFile(
    new URL("../../src/features/private-gateways/renderPrivateGateways.js", import.meta.url),
    "utf8",
  );
  assert.match(
    renderer,
    /deployment\.status !== "active"[\s\S]*注册后可用[\s\S]*data-private-gateway-brand/u,
  );
});

function writeInput(extra) {
  return {
    controlApiUrl: CONTROL_URL,
    expectedRevision: 2,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
    ...extra,
  };
}

function updateCommand() {
  return {
    action: "update",
    product_name: "MetaHub Enterprise",
    primary_color: "#176B4D",
    secondary_color: "#245F9A",
    login_title: "Secure access",
    login_message: "Host-scoped access",
  };
}

function commonBrand(overrides = {}) {
  return {
    status: "ready",
    product_name: "MetaHub Enterprise",
    primary_color: "#176B4D",
    secondary_color: "#245F9A",
    login_title: "Secure access",
    login_message: "Host-scoped access",
    logo: asset("logo"),
    favicon: asset("favicon"),
    revision: 2,
    source_sha256: "c".repeat(64),
    actions: ["update", "attach_asset", "remove_asset"],
    updated_at: NOW,
    ...overrides,
  };
}

function platformBrand(overrides = {}) {
  return commonBrand({
    platform_brand_id: PLATFORM_ID,
    domain: domain(),
    published_generation: 1,
    lkg_generation: 1,
    actions: ["update", "attach_asset", "remove_asset", "request_domain", "release"],
    ...overrides,
  });
}

function channelBrand(overrides = {}) {
  return commonBrand({
    channel_brand_id: CHANNEL_ID,
    domain: domain(),
    contract_status: "active",
    contract_sha256: "d".repeat(64),
    contract_revision: 3,
    partner_scope_revision: 4,
    published_generation: 1,
    lkg_generation: 1,
    actions: ["update", "attach_asset", "remove_asset", "request_domain", "release"],
    ...overrides,
  });
}

function deploymentBrand(overrides = {}) {
  return commonBrand({
    deployment_brand_id: DEPLOYMENT_BRAND_ID,
    status: "published",
    desired_generation: 1,
    applied_generation: 1,
    lkg_generation: 1,
    ack_status: "applied",
    actions: ["update", "attach_asset", "remove_asset", "publish", "rollback_lkg"],
    ...overrides,
  });
}

function asset(kind) {
  return {
    asset_id: ASSET_ID,
    kind,
    status: "published",
    byte_count: 128,
    width: 64,
    height: 64,
    content_sha256: "e".repeat(64),
  };
}

function domain() {
  return {
    domain_id: DOMAIN_ID,
    canonical_domain: "brand.example.com",
    status: "active",
    observation_count: 2,
    certificate_ready: true,
    routing_ready: true,
    revision: 2,
    challenge: null,
  };
}

function publicBrand(overrides = {}) {
  return {
    product_name: "MetaHub Enterprise",
    primary_color: "#176B4D",
    secondary_color: "#245F9A",
    login_title: "Secure access",
    login_message: "Host-scoped access",
    logo_path: `/branding/public/assets/${ASSET_ID}`,
    favicon_path: `/branding/public/assets/${ASSET_ID}`,
    brand_generation: 3,
    public_etag: `"brand-${"f".repeat(64)}"`,
    ...overrides,
  };
}

function envelope(branding) {
  return { branding, idempotent_replay: false, request_id: "req_brand", trace_id: "trc_brand" };
}

function brandResponse(branding, replay = false) {
  return new Response(JSON.stringify({ ...envelope(branding), idempotent_replay: replay }), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "private, no-store",
      ETag: `"${branding.revision}"`,
    },
  });
}

function publicResponse(branding) {
  return new Response(JSON.stringify({
    branding, request_id: "req_public_brand", trace_id: "trc_public_brand",
  }), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      ETag: branding.public_etag,
    },
  });
}

function platformContext() {
  return {
    operator: {
      operator_id: `pto_${"0".repeat(32)}`,
      status: "active",
      revision: 7,
      roles: ["platform_owner"],
    },
    permissions: [
      "platform.brand.read", "platform.brand.manage",
      "platform.channel_brand.read", "platform.channel_brand.manage",
    ],
    expires_at: "2026-09-01T12:00:00.000Z",
  };
}

function installDocument(t) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: { cookie: `__Host-metahub-platform-csrf=${PLATFORM_CSRF}; __Host-metahub-workspace-csrf=${WORKSPACE_CSRF}` },
  });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, "document", descriptor);
    else delete globalThis.document;
  });
}

function installFetch(t, responder) {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return responder(url, options);
  };
  t.after(() => { globalThis.fetch = previous; });
  return calls;
}
