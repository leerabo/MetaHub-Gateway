import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperContracts } from "../../src/features/developer-contracts/renderDeveloperContracts.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  buildPlatformDeveloperContract,
  downloadDeveloperSdk,
  getDeveloperContractWorkspace,
  getPlatformDeveloperContracts,
} from "../../src/lib/developerContracts.js";
import { PlatformApiError } from "../../src/lib/platform.js";

const CONTROL_URL = "http://control.local";
const TOKEN = "developer-test-token-0001";
const CSRF = "C".repeat(43);
const IDEMPOTENCY = "developer-contract-console-0001";
const API_ID = `apv_${"1".repeat(32)}`;
const NEXT_API_ID = `apv_${"2".repeat(32)}`;
const SDK_ID = `sdr_${"3".repeat(32)}`;
const JS_SDK_ID = `sdr_${"4".repeat(32)}`;
const PENDING_PY_SDK_ID = `sdr_${"8".repeat(32)}`;
const PENDING_JS_SDK_ID = `sdr_${"9".repeat(32)}`;
const NOTICE_ID = `dpn_${"5".repeat(32)}`;
const SCHEMA_ID = `wsv_${"6".repeat(32)}`;
const EVENT_ID = `dce_${"7".repeat(32)}`;
const NOW = "2026-08-29T08:00:00.000Z";
const SUNSET = "2026-12-01T08:00:00.000Z";
const ZIP = new Uint8Array([80, 75, 3, 4, 20, 0, 1, 2, 3, 4]);
const ARTIFACT_SHA = createHash("sha256").update(ZIP).digest("hex");
const ARTIFACT_B64 = createHash("sha256").update(ZIP).digest("base64");
const SIGNING_KEY = await webcrypto.subtle.generateKey(
  { name: "Ed25519" }, true, ["sign", "verify"],
);
const PUBLIC_KEY_B64 = Buffer.from(
  await webcrypto.subtle.exportKey("raw", SIGNING_KEY.publicKey),
).toString("base64url");
const SDK_SIGNATURES = Object.fromEntries(await Promise.all(
  ["python", "javascript"].map(async (language) => {
    const payload = new TextEncoder().encode(JSON.stringify({
      artifact_sha256: ARTIFACT_SHA,
      contract_sha256: "a".repeat(64),
      language,
      purpose: "metahub.public-sdk.release/v1",
      sdk_version: "1.0.0",
    }));
    const signature = Buffer.from(await webcrypto.subtle.sign(
      { name: "Ed25519" }, SIGNING_KEY.privateKey, payload,
    ));
    return [language, {
      signature_b64: signature.toString("base64url"),
      signature_sha256: createHash("sha256").update(signature).digest("hex"),
    }];
  }),
));

test("Developer and Platform clients isolate credentials and enforce exact release writes", async (t) => {
  installGlobals(t);
  const calls = installFetch(t, (url, options) => routeResponse(new URL(url).pathname, options));

  const developer = await getDeveloperContractWorkspace({
    controlApiUrl: CONTROL_URL,
    token: TOKEN,
  });
  assert.equal(developer.versions[0].version, "1.0.0");
  assert.equal(developer.changelog.entries[0].metadata.migration_guide_code, "api-v1-to-v1.1");
  assert.equal(developer.sdk_releases.length, 2);

  const platform = await getPlatformDeveloperContracts({ controlApiUrl: CONTROL_URL });
  assert.equal(platform.deprecations[0].migration_guide_code, "api-v1-to-v1.1");
  await buildPlatformDeveloperContract({
    controlApiUrl: CONTROL_URL,
    apiVersion: "1.1.0",
    pythonSdkVersion: "1.1.0",
    javascriptSdkVersion: "1.1.0",
    producerRevision: 45,
    confirmed: true,
    idempotencyKey: IDEMPOTENCY,
  });

  const developerCalls = calls.slice(0, 5);
  developerCalls.forEach(({ options }) => {
    assert.equal(options.headers.Authorization, `Bearer ${TOKEN}`);
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal("X-CSRF-Token" in options.headers, false);
  });
  assert.equal(calls[5].options.credentials, "include");
  assert.equal(calls[5].options.headers.Authorization, undefined);
  assert.equal(calls[6].options.credentials, "include");
  assert.equal(calls[6].options.headers["X-CSRF-Token"], CSRF);
  assert.equal(calls[6].options.headers["Idempotency-Key"], IDEMPOTENCY);
  assert.deepEqual(JSON.parse(calls[6].options.body), {
    api_version: "1.1.0",
    python_sdk_version: "1.1.0",
    javascript_sdk_version: "1.1.0",
    producer_revision: 45,
    confirmed: true,
  });
});

test("SDK download verifies headers, bytes, receipt, and Ed25519 signature", async (t) => {
  installGlobals(t);
  const calls = installFetch(t, () => zipResponse());
  const proof = publishedSdk("python");
  const result = await downloadDeveloperSdk({
    controlApiUrl: CONTROL_URL,
    token: TOKEN,
    language: "python",
    sdkVersion: "1.0.0",
    contractSha256: proof.contract_sha256,
    artifactSha256: ARTIFACT_SHA,
    artifactReceiptSha256: "c".repeat(64),
    signatureB64: proof.signature_b64,
    signatureSha256: proof.signature_sha256,
    publicKeyB64: proof.public_key_b64,
  });
  assert.equal(result.filename, "metahub-python-sdk-1.0.0.zip");
  assert.equal(result.blob.size, ZIP.byteLength);
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(calls[0].options.credentials, "omit");

  globalThis.fetch = async () => zipResponse({ receipt: "d".repeat(64) });
  await assert.rejects(
    () => downloadDeveloperSdk({
      controlApiUrl: CONTROL_URL,
      token: TOKEN,
      language: "python",
      sdkVersion: "1.0.0",
      contractSha256: proof.contract_sha256,
      artifactSha256: ARTIFACT_SHA,
      artifactReceiptSha256: "c".repeat(64),
      signatureB64: proof.signature_b64,
      signatureSha256: proof.signature_sha256,
      publicKeyB64: proof.public_key_b64,
    }),
    (error) => error instanceof AuthApiError
      && error.code === "developer_contract.response_invalid",
  );

  const wrongSignature = publishedSdk("javascript");
  globalThis.fetch = async () => zipResponse();
  await assert.rejects(
    () => downloadDeveloperSdk({
      controlApiUrl: CONTROL_URL,
      token: TOKEN,
      language: "python",
      sdkVersion: "1.0.0",
      contractSha256: proof.contract_sha256,
      artifactSha256: ARTIFACT_SHA,
      artifactReceiptSha256: "c".repeat(64),
      signatureB64: wrongSignature.signature_b64,
      signatureSha256: wrongSignature.signature_sha256,
      publicKeyB64: wrongSignature.public_key_b64,
    }),
    (error) => error instanceof AuthApiError
      && error.code === "developer_contract.response_invalid",
  );
});

test("strict clients reject top-level expansion and public SDK owner drift", async (t) => {
  installGlobals(t);
  installFetch(t, (url, options) => {
    const response = routeResponse(new URL(url).pathname, options);
    if (new URL(url).pathname === "/developer/api-versions") {
      return jsonResponse({ ...apiVersionList(), internal_routes: ["/platform/operators"] });
    }
    return response;
  });
  await assert.rejects(
    () => getDeveloperContractWorkspace({ controlApiUrl: CONTROL_URL, token: TOKEN }),
    (error) => error instanceof AuthApiError
      && error.code === "developer_contract.response_invalid",
  );

  globalThis.fetch = async (url, options) => {
    if (new URL(url).pathname === "/developer/sdks/python") {
      return jsonResponse(sdkList("javascript"));
    }
    return routeResponse(new URL(url).pathname, options);
  };
  await assert.rejects(
    () => getDeveloperContractWorkspace({ controlApiUrl: CONTROL_URL, token: TOKEN }),
    (error) => error.code === "developer_contract.response_invalid",
  );
});

test("Developer and Platform render public lifecycle proof without private keys or internal routes", () => {
  const developerHtml = renderDeveloperContracts(developerState());
  assert.match(developerHtml, /API 版本与 SDK/u);
  assert.match(developerHtml, /迁移指南/u);
  assert.match(developerHtml, /api-v1-to-v1\.1/u);
  assert.match(developerHtml, /data-developer-sdk-download/u);
  assert.match(developerHtml, /通过 \/ 通过/u);
  assert.doesNotMatch(developerHtml, /private[_ -]?key|\/platform\/developer-contracts|Authorization|Bearer/iu);

  const root = { innerHTML: "" };
  renderPlatformWorkspace(root, platformState(platformWorkspace()));
  assert.match(root.innerHTML, /Contracts and SDKs/u);
  assert.match(root.innerHTML, /Canonical diff and approval/u);
  assert.match(root.innerHTML, /data-platform-developer-contract-build-form/u);
  assert.match(root.innerHTML, /data-platform-developer-contract-approve-form/u);
  assert.match(root.innerHTML, /Signature summary/u);
  assert.match(root.innerHTML, /api-v1-to-v1\.1/u);
  assert.doesNotMatch(root.innerHTML, /private[_ -]?key|\/internal\/|signature_b64|Authorization|Bearer/iu);
});

test("Platform release controller commits current builds and clears workspace after dynamic revoke", async (t) => {
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash: "#releases" },
    history: { replaceState() {} },
  };
  t.after(() => { globalThis.window = originalWindow; });
  let permissions = [
    "platform.developer_contract.read",
    "platform.developer_contract.manage",
    "platform.developer_contract.approve",
  ];
  let workspace = platformWorkspace();
  let revoked = false;
  const calls = [];
  const api = {
    session: async () => ({ context: {
      permissions: [...permissions],
      operator: { display_name: "Release", roles: ["release_manager"] },
    } }),
    navigation: async () => ({
      items: permissions.includes("platform.developer_contract.read")
        ? [{ id: "releases", label: "Releases", href: "/platform.html#releases" }] : [],
    }),
    developerContracts: async () => workspace,
    buildDeveloperContract: async (input) => {
      calls.push(input);
      if (revoked) {
        permissions = [];
        throw new PlatformApiError({
          status: 403,
          code: "platform.permission_denied",
          message: "Permission denied",
          requestId: "req_denied",
        });
      }
      workspace = platformWorkspace({
        versions: [
          pendingApiVersion({ version: "1.1.0", api_version_id: NEXT_API_ID }),
          ...workspace.versions,
        ],
      });
      return buildResponse();
    },
  };
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api,
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  assert.equal(await controller.mount(), true);
  assert.match(root.innerHTML, /Contracts and SDKs/u);
  assert.equal(await controller.buildDeveloperContract({
    apiVersion: "1.1.0",
    pythonSdkVersion: "1.1.0",
    javascriptSdkVersion: "1.1.0",
    producerRevision: 45,
    confirmed: true,
  }), true);
  assert.match(root.innerHTML, /1\.1\.0/u);
  assert.equal(calls[0].idempotencyKey, IDEMPOTENCY);

  revoked = true;
  assert.equal(await controller.buildDeveloperContract({
    apiVersion: "1.2.0",
    pythonSdkVersion: "1.2.0",
    javascriptSdkVersion: "1.2.0",
    producerRevision: 46,
    confirmed: true,
  }), false);
  assert.doesNotMatch(root.innerHTML, /Contracts and SDKs/u);
  assert.doesNotMatch(root.innerHTML, /api-v1-to-v1\.1/u);
  controller.destroy();
  assert.equal(root.innerHTML, "");
});

test("PRD-133 source keeps existing navigation, responsive views, and credential domains", async () => {
  const [main, developerRender, platformStateSource, platformRender, client, css, platformCss, access] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-contracts/renderDeveloperContracts.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/renderPlatformPage.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/developerContracts.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/platform_access/service.py", import.meta.url), "utf8"),
  ]);
  assert.match(main, /case "api-docs":[\s\S]*getDeveloperContractWorkspace/u);
  assert.match(main, /status === 403[\s\S]*await refresh\(\)/u);
  assert.match(developerRender, /migration_guide_code/u);
  assert.match(platformStateSource, /state\.view === "releases"[\s\S]*loadDeveloperContracts/u);
  assert.match(platformRender, /compatibility_outcome|smoke_matrix|signature_sha256/u);
  assert.match(client, /credentials: "omit"[\s\S]*credentials: "include"/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|indexedDB/u);
  assert.match(css, /@media \(max-width: 420px\)[\s\S]*developer-contracts/u);
  assert.match(platformCss, /@media \(max-width: 600px\)[\s\S]*platform-developer-contract/u);
  assert.match(access, /item_id == "releases"[\s\S]*platform\.developer_contract\.read/u);
});

function routeResponse(path, options) {
  if (path === "/developer/api-versions") return jsonResponse(apiVersionList());
  if (path === "/developer/changelog") return jsonResponse(changelog());
  if (path === "/developer/webhook-schemas") return jsonResponse(webhookSchemas());
  if (path === "/developer/sdks/python") return jsonResponse(sdkList("python"));
  if (path === "/developer/sdks/javascript") return jsonResponse(sdkList("javascript"));
  if (path === "/platform/developer-contracts" && options.method === "GET") {
    return jsonResponse(platformWorkspace());
  }
  if (path === "/platform/developer-contracts/build") return jsonResponse(buildResponse(), 201);
  throw new Error(`unexpected route ${path}`);
}

function developerState() {
  return {
    versions: [publishedApiVersion()],
    changelog: changelog(),
    schemas: webhookSchemas().schemas,
    sdk_releases: [publishedSdk("python"), publishedSdk("javascript")],
    request_ids: ["req_versions"],
  };
}

function apiVersionList() {
  return traced({ versions: [publishedApiVersion()] }, "versions");
}

function publishedApiVersion(overrides = {}) {
  return {
    api_version_id: API_ID,
    version: "1.0.0",
    registry_revision: 1,
    status: "published",
    revision: 2,
    contract_sha256: "a".repeat(64),
    compatibility_baseline_sha256: null,
    compatibility_outcome: "compatible",
    breaking_reasons: [],
    created_at: NOW,
    published_at: NOW,
    deprecated_at: null,
    sunset_at: null,
    ...overrides,
  };
}

function pendingApiVersion(overrides = {}) {
  return publishedApiVersion({
    api_version_id: NEXT_API_ID,
    version: "1.1.0",
    status: "pending_approval",
    revision: 1,
    compatibility_baseline_sha256: "a".repeat(64),
    published_at: null,
    ...overrides,
  });
}

function sdkList(language) {
  return traced({ releases: [publishedSdk(language)] }, `sdk_${language}`);
}

function publishedSdk(language, overrides = {}) {
  return {
    sdk_release_id: language === "python" ? SDK_ID : JS_SDK_ID,
    api_version: "1.0.0",
    language,
    sdk_version: "1.0.0",
    runtime_version: language === "python" ? ">=3.11" : ">=20",
    contract_sha256: "a".repeat(64),
    artifact_sha256: ARTIFACT_SHA,
    artifact_receipt_sha256: "c".repeat(64),
    file_manifest: { "README.md": "d".repeat(64) },
    smoke_matrix: { contract_versions: ["1.0.0", "N-1"], linux: "passed", windows: "passed" },
    status: "published",
    revision: 2,
    signing_key_id: "developer-contract-test-v1",
    signature_b64: SDK_SIGNATURES[language].signature_b64,
    signature_sha256: SDK_SIGNATURES[language].signature_sha256,
    public_key_b64: PUBLIC_KEY_B64,
    download_url: `/developer/sdks/${language}/1.0.0/download`,
    created_at: NOW,
    published_at: NOW,
    ...overrides,
  };
}

function pendingSdk(language) {
  return publishedSdk(language, {
    sdk_release_id: language === "python" ? PENDING_PY_SDK_ID : PENDING_JS_SDK_ID,
    api_version: "1.1.0",
    sdk_version: "1.1.0",
    contract_sha256: "f".repeat(64),
    smoke_matrix: { contract_versions: ["1.1.0", "N-1"], linux: "passed", windows: "passed" },
    status: "pending_approval",
    revision: 1,
    signing_key_id: null,
    signature_b64: null,
    signature_sha256: null,
    public_key_b64: null,
    download_url: null,
    published_at: null,
  });
}

function changelog() {
  return traced({
    entries: [{
      event_id: EVENT_ID,
      event_type: "api.deprecated",
      source_object_type: "deprecation_notice",
      source_object_id: NOTICE_ID,
      source_revision: 2,
      metadata: {
        api_version: "1.0.0",
        migration_guide_code: "api-v1-to-v1.1",
        notice_id: NOTICE_ID,
        operation_count: 1,
        replacement_version: "1.1.0",
        sunset_at: SUNSET,
      },
      occurred_at: NOW,
    }],
    next_cursor: null,
  }, "changelog");
}

function webhookSchemas() {
  return traced({ schemas: [{
    webhook_schema_version_id: SCHEMA_ID,
    api_version: "1.0.0",
    event_name: "api.deprecated",
    schema_version: 1,
    json_schema: { type: "object", additionalProperties: false },
    schema_sha256: "8".repeat(64),
    sample_sha256: "9".repeat(64),
    published_at: NOW,
  }] }, "schemas");
}

function notice() {
  return {
    deprecation_notice_id: NOTICE_ID,
    api_version_id: API_ID,
    api_version: "1.0.0",
    replacement_api_version_id: NEXT_API_ID,
    replacement_api_version: "1.1.0",
    affected_operations: ["developer.api_versions.list"],
    migration_guide_code: "api-v1-to-v1.1",
    status: "draft",
    minimum_notice_days: 90,
    adoption_watermark: 0,
    notification_watermark: 0,
    sunset_at: SUNSET,
    revision: 1,
    created_at: NOW,
    announced_at: null,
    decided_at: null,
  };
}

function platformWorkspace(overrides = {}) {
  return traced({
    versions: [pendingApiVersion(), publishedApiVersion({ api_version_id: API_ID })],
    sdk_releases: [pendingSdk("python"), pendingSdk("javascript"), publishedSdk("python")],
    deprecations: [notice()],
    ...overrides,
  }, "platform_workspace");
}

function buildResponse() {
  return traced({
    api_version: pendingApiVersion(),
    sdk_releases: [pendingSdk("python"), pendingSdk("javascript")],
    idempotent_replay: false,
  }, "build");
}

function platformState(workspace) {
  return {
    loading: false,
    session: { context: {
      permissions: [
        "platform.developer_contract.read",
        "platform.developer_contract.manage",
        "platform.developer_contract.approve",
      ],
      operator: { display_name: "Release", roles: ["release_manager"] },
    } },
    navigation: [{ id: "releases", label: "Releases", href: "/platform.html#releases" }],
    view: "releases",
    developerContracts: workspace,
    developerContractIntent: null,
    error: null,
    feedback: null,
    pendingAction: null,
  };
}

function traced(value, suffix) {
  return { ...value, request_id: `req_${suffix}`, trace_id: `trc_${suffix}` };
}

function zipResponse({ receipt = "c".repeat(64) } = {}) {
  return new Response(ZIP, {
    status: 200,
    headers: {
      "Cache-Control": "no-store, private",
      "Content-Disposition": 'attachment; filename="metahub-python-sdk-1.0.0.zip"',
      "Content-Length": String(ZIP.byteLength),
      "Content-Type": "application/zip",
      Digest: `sha-256=${ARTIFACT_B64}`,
      "X-Artifact-Receipt-SHA256": receipt,
      "X-Content-Type-Options": "nosniff",
      "X-Request-ID": "req_download",
    },
  });
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Cache-Control": "no-store, private",
      "Content-Type": "application/json",
      "X-Request-ID": payload.request_id ?? "req_test",
    },
  });
}

function installGlobals(t) {
  const originalDocument = globalThis.document;
  const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${CSRF}` };
  if (!globalThis.crypto?.subtle) {
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
  }
  t.after(() => {
    globalThis.document = originalDocument;
    if (cryptoDescriptor) Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
    else delete globalThis.crypto;
  });
}

function installFetch(t, handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return handler(String(url), options);
  };
  t.after(() => { globalThis.fetch = original; });
  return calls;
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}
