import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
  createProviderFromCatalog,
  getProviderCatalogCompatibility,
  listProviderCatalog,
  preflightCatalogProvider,
} from "../../src/lib/providerCatalog.js";
import {
  getPlatformProviderCatalog,
  publishPlatformProviderCatalog,
} from "../../src/lib/platform.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import { renderProviderPanel } from "../../src/features/providers/renderProviderPanel.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import { withPermissionSnapshot } from "./permissionTestContext.mjs";

const TOKEN = "workspace-token";
const SIGNATURE = "s".repeat(86);
const MANIFEST_HASH = "a".repeat(64);
const SIGNATURE_HASH = "b".repeat(64);
const NOW = "2026-08-26T08:00:00Z";

test("enterprise catalog client sends only signed coordinates and bounded filters", async () => {
  const calls = installFetch((url, options) => {
    if (String(url).includes("/compatibility")) {
      return jsonResponse({
        entry_code: "deepseek", version: "2026.08.26.1", status: "active",
        compatibility: compatibility("native"), current_instances: 1,
        request_id: "req-compat", trace_id: "trc-compat",
      });
    }
    if (String(url).endsWith("/providers/from-catalog")) {
      return jsonResponse({
        instance: instance(), idempotent_replay: false,
        next_action: "configure_provider_key", request_id: "req-create", trace_id: "trc-create",
      }, 201);
    }
    if (String(url).endsWith("/preflight")) {
      return jsonResponse({
        run: preflight(), idempotent_replay: false,
        request_id: "req-preflight", trace_id: "trc-preflight",
      });
    }
    return jsonResponse(catalogResponse());
  });

  await listProviderCatalog({
    controlApiUrl: "http://control.local",
    token: TOKEN,
    filters: { status: "active", vendorCode: "deepseek", capabilityCode: "chat", regionCode: "cn-mainland" },
  });
  await getProviderCatalogCompatibility({
    controlApiUrl: "http://control.local", token: TOKEN, entryCode: "deepseek",
  });
  await createProviderFromCatalog({
    controlApiUrl: "http://control.local", token: TOKEN,
    entryCode: "deepseek", version: "2026.08.26.1", regionCode: "cn-mainland",
    capabilityCodes: ["chat"], providerCode: "deepseek_catalog", name: "DeepSeek catalog",
    confirmed: true, idempotencyKey: "console:prd119:create:0001",
  });
  await preflightCatalogProvider({
    controlApiUrl: "http://control.local", token: TOKEN, providerId: "provider_deepseek",
    timeoutSeconds: 5, idempotencyKey: "console:prd119:preflight:0001",
  });

  const listUrl = new URL(calls[0].url);
  assert.deepEqual(Object.fromEntries(listUrl.searchParams), {
    status: "active", vendor_code: "deepseek", capability_code: "chat", region_code: "cn-mainland",
  });
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(calls[1].url, "http://control.local/provider-catalog/deepseek/compatibility");
  const createBody = JSON.parse(calls[2].options.body);
  assert.deepEqual(createBody, {
    entry_code: "deepseek", version: "2026.08.26.1", region_code: "cn-mainland",
    capability_codes: ["chat"], provider_code: "deepseek_catalog",
    name: "DeepSeek catalog", confirmed: true,
  });
  assert.equal(new Headers(calls[2].options.headers).get("Idempotency-Key"), "console:prd119:create:0001");
  assert.deepEqual(JSON.parse(calls[3].options.body), { timeout_seconds: 5 });
  for (const call of calls) {
    assert.equal(call.options.credentials, "omit");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.redirect, "error");
  }
  const serialized = JSON.stringify(calls.map((call) => ({ url: call.url, body: call.options.body })));
  assert.doesNotMatch(serialized, /tenant_id|workspace_id|base_url|plain_value|credential|secret/iu);
});

test("Provider page exposes official, local, and migration catalog modes without endpoint or secret fields", () => {
  const auth = withPermissionSnapshot(
    { user: { role: "owner" } },
    [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE, PERMISSIONS.PROVIDER_CATALOG_READ],
  );
  const html = renderProviderPanel({
    state: "ok",
    providers: [],
    providerKeysByProviderId: {},
    providerCatalog: {
      ...catalogResponse(),
      state: "ok",
      activeTab: "catalog",
      filters: {},
      compatibilityByEntryCode: {
        deepseek: {
          compatibility: compatibility("native"), current_instances: 1,
        },
      },
      lastCreated: { instance: instance(), next_action: "configure_provider_key" },
      lastPreflight: { run: preflight() },
    },
  }, auth);

  assert.match(html, /选择模型厂家/u);
  assert.match(html, /data-provider-catalog-create-form/u);
  assert.match(html, /href="console.html#workspace.gateway.private-endpoints"/u);
  assert.match(html, /仅说明迁移兼容性/u);
  assert.match(html, /data-provider-catalog-preflight-form/u);
  assert.match(html, /下一步：配置客户自持 Provider Key/u);
  assert.doesNotMatch(html, /api\.deepseek\.com|sk-[A-Za-z0-9]|Authorization:/u);
  assert.doesNotMatch(html, /<unsafe>/u);
  assert.match(html, /&lt;unsafe&gt;/u);

  const configured = renderProviderPanel({
    state: "ok", providers: [], providerKeysByProviderId: {},
    providerCatalog: { ...catalogResponse(), state: "ok", activeTab: "providers" },
  }, auth);
  assert.match(configured, /create-provider-form/u);
  assert.match(configured, /data-provider-tab="catalog">添加模型服务/u);
});

test("Platform catalog client uses Cookie, CSRF, CAS, and no workspace authorization", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"c".repeat(43)}` };
  try {
    const calls = installFetch((url) => String(url).endsWith("/versions")
      ? jsonResponse({
          catalog: platformVersion(2), idempotent_replay: false, propagated: true,
          request_id: "req-publish", trace_id: "trc-publish",
        }, 201)
      : jsonResponse(platformCatalogResponse(false)));
    await getPlatformProviderCatalog({ controlApiUrl: "http://control.local" });
    await publishPlatformProviderCatalog({
      controlApiUrl: "http://control.local",
      manifest: publishManifest(),
      keyId: "provider-catalog-release-v2",
      signature: SIGNATURE,
      reasonCode: "catalog_release",
      confirmed: true,
      expectedRevision: 1,
      idempotencyKey: "platform:provider-catalog:0001",
    });

    assert.equal(calls[0].options.credentials, "include");
    assert.equal(calls[0].options.headers.Authorization, undefined);
    assert.equal(calls[1].options.headers["X-CSRF-Token"], "c".repeat(43));
    assert.equal(calls[1].options.headers["If-Match"], '"1"');
    assert.equal(calls[1].options.headers["Idempotency-Key"], "platform:provider-catalog:0001");
    const body = JSON.parse(calls[1].options.body);
    assert.deepEqual(Object.keys(body).sort(), ["confirmed", "key_id", "manifest", "reason_code", "signature"]);
    assert.equal(body.manifest.entries.length, 10);
    assert.doesNotMatch(JSON.stringify(calls[1].options.headers), /Bearer|workspace-token/u);
  } finally {
    globalThis.document = originalDocument;
  }
});

test("Platform render separates security projection from release management", () => {
  const securityRoot = fakeRoot();
  renderPlatformWorkspace(securityRoot, platformState(
    ["platform.provider_catalog.security_read"], platformCatalogResponse(true),
  ));
  assert.match(securityRoot.innerHTML, /Security projection/u);
  assert.match(securityRoot.innerHTML, /Signature proof/u);
  assert.doesNotMatch(securityRoot.innerHTML, /data-platform-provider-catalog-form/u);

  const releaseRoot = fakeRoot();
  renderPlatformWorkspace(releaseRoot, platformState(
    ["platform.provider_catalog.manage"], platformCatalogResponse(false),
  ));
  assert.match(releaseRoot.innerHTML, /data-platform-provider-catalog-form/u);
  assert.match(releaseRoot.innerHTML, /Canonical manifest/u);
  assert.doesNotMatch(releaseRoot.innerHTML, /private[_ -]?key|tenant_id|workspace_id/iu);
});

test("Platform controller loads and publishes catalog without retaining submitted signing payload", async () => {
  let catalogGeneration = 1;
  const calls = [];
  const api = {
    session: async () => ({ context: {
      permissions: ["platform.provider_catalog.manage"],
      operator: { display_name: "Release", roles: ["release_manager"] },
    } }),
    navigation: async () => ({ items: [{
      id: "provider-catalog", label: "Provider catalog", href: "/platform.html#provider-catalog",
    }] }),
    providerCatalog: async () => platformCatalogResponse(false, catalogGeneration),
    publishProviderCatalog: async (input) => {
      calls.push(input);
      catalogGeneration = 2;
      return {
        catalog: platformVersion(2), idempotent_replay: false, propagated: true,
        request_id: "req", trace_id: "trc",
      };
    },
  };
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api,
    newIdempotencyKey: () => "platform:provider-catalog:controller",
  });
  assert.equal(await controller.mount(), true);
  controller.selectView("provider-catalog");
  assert.match(root.innerHTML, /Publish signed manifest/u);
  assert.equal(await controller.publishProviderCatalog({
    manifestText: JSON.stringify(publishManifest()),
    keyId: "provider-catalog-release-v2",
    signature: SIGNATURE,
    reasonCode: "catalog_release",
    confirmed: true,
  }), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].expectedRevision, 1);
  assert.equal(calls[0].idempotencyKey, "platform:provider-catalog:controller");
  assert.doesNotMatch(root.innerHTML, new RegExp(SIGNATURE, "u"));
  assert.doesNotMatch(root.innerHTML, /entry_9/u);
  assert.match(root.innerHTML, /Generation[\s\S]*2/u);
});

test("PRD-119 source keeps enterprise and Platform DOM and credential domains separate", async () => {
  const [enterprise, platformRender, platformClient, mainSource, css] = await Promise.all([
    readFile(new URL("../../src/features/providers/renderProviderCatalog.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/renderPlatformPage.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/platform.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(enterprise, /data-platform-provider-catalog/u);
  assert.doesNotMatch(platformRender, /data-provider-catalog-create-form|plainValue|Provider Key/u);
  assert.match(platformClient, /credentials: "include"/u);
  assert.match(mainSource, /loadProvidersState\(\{ includeCatalog: true \}\)/u);
  assert.match(mainSource, /providerCatalogFeedback = "筛选已应用"/u);
  assert.doesNotMatch(mainSource, /providerCatalogFeedback = "正在筛选"/u);
  assert.match(css, /@media \(max-width: 900px\)[\s\S]*\.provider-catalog-filters/u);
});

function catalogResponse() {
  return {
    version: "2026.08.26.1", generation: 1, manifest_sha256: MANIFEST_HASH,
    published_at: NOW,
    entries: [
      officialEntry(),
      {
        ...officialEntry(), entry_code: "local_openai_compatible", vendor_code: "customer_local",
        display_name: "Local OpenAI-compatible", provider_type: "local_endpoint",
        service_class: "local_template", auth_modes: ["bearer", "none"],
        data_regions: ["customer-network"], compliance_tags: ["customer_managed"],
        instantiation_mode: "local_endpoint", endpoints: [],
        compatibility: compatibility("openai_compatible"),
      },
      {
        ...officialEntry(), entry_code: "openrouter_migration", vendor_code: "openrouter",
        display_name: "<unsafe>", provider_type: "migration_compatible",
        service_class: "migration_source", status: "deprecated",
        data_regions: ["migration-only"], compliance_tags: ["migration_source_only"],
        instantiation_mode: "migration_only", endpoints: [],
        compatibility: compatibility("migration_only"),
      },
    ],
    request_id: "req-catalog", trace_id: "trc-catalog",
  };
}

function officialEntry() {
  return {
    entry_code: "deepseek", vendor_code: "deepseek", display_name: "DeepSeek",
    provider_type: "deepseek", service_class: "official_cloud", auth_modes: ["bearer"],
    data_regions: ["cn-mainland"], compliance_tags: ["cn_mainland", "customer_credential"],
    status: "active", instantiation_mode: "provider",
    capabilities: [{
      code: "chat", max_context_tokens: 128000, supports_stream: true,
      supports_tools: true, supports_files: false,
    }],
    endpoints: [{
      region_code: "cn-mainland", cloud_service_code: "deepseek_official",
      scheme: "https", host: "api.deepseek.com", path_template: "/v1",
      residency_tags: ["cn-mainland"],
    }],
    compatibility: compatibility("native"),
  };
}

function compatibility(level) {
  return {
    level,
    doc_code: `provider_catalog.deepseek.${level}`,
    migration_source_code: level === "migration_only" ? "openrouter" : null,
    request_difference_codes: [],
  };
}

function instance() {
  return {
    provider_catalog_instance_id: `pci_${"1".repeat(32)}`,
    workspace_id: `wsp_${"2".repeat(32)}`,
    provider_id: "provider_deepseek", provider_code: "deepseek_catalog",
    entry_code: "deepseek", catalog_version: "2026.08.26.1", region_code: "cn-mainland",
    capability_codes: ["chat"], revision: 1, created_at: NOW,
  };
}

function preflight() {
  return {
    provider_preflight_run_id: `pfr_${"3".repeat(32)}`,
    provider_id: "provider_deepseek", entry_code: "deepseek",
    catalog_version: "2026.08.26.1", region_code: "cn-mainland",
    status: "succeeded", outcome_code: "reachable", latency_ms: 48, http_status: 401,
    capability_sha256: "c".repeat(64), checked_at: NOW,
  };
}

function publishManifest() {
  return {
    schema_version: 1,
    version: "2026.08.27.1",
    entries: Array.from({ length: 10 }, (_, index) => ({ entry_code: `entry_${index}` })),
  };
}

function platformVersion(generation = 1) {
  return {
    version: generation === 1 ? "2026.08.26.1" : "2026.08.27.1",
    generation, manifest_sha256: MANIFEST_HASH, key_id: "provider-catalog-release-v2",
    signature_algorithm: "ed25519", signature_sha256: SIGNATURE_HASH,
    entry_count: 10, compatibility_change_count: generation - 1,
    published_by_operator_id: generation === 1 ? null : `pto_${"4".repeat(32)}`,
    published_at: NOW,
  };
}

function platformCatalogResponse(securityProjection, generation = 1) {
  return {
    current: platformVersion(generation), recent_versions: [platformVersion(generation)],
    security_projection: securityProjection, request_id: "req-platform-catalog", trace_id: "trc-platform-catalog",
  };
}

function platformState(permissions, providerCatalog) {
  return {
    session: { context: {
      permissions,
      operator: { display_name: "Operator", roles: ["release_manager"] },
    } },
    navigation: [{ id: "provider-catalog", label: "Provider catalog", href: "#provider-catalog" }],
    view: "provider-catalog", providerCatalog, loading: false,
  };
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}

function installFetch(responder) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return responder(url, options);
  };
  return calls;
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    async json() { return structuredClone(payload); },
  };
}
