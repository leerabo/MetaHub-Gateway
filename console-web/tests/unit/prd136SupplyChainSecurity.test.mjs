import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperContracts } from "../../src/features/developer-contracts/renderDeveloperContracts.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import {
  applyPlatformSecurityException,
  getDeveloperSupplyChainSecurity,
  getPlatformSupplyChainSecurity,
} from "../../src/lib/supplyChainSecurity.js";

const CONTROL_URL = "http://control.local";
const TOKEN = "developer-supply-chain-token-0001";
const CSRF = "C".repeat(43);
const IDEMPOTENCY = "supply-chain-console-command-0001";
const RELEASE_ID = `sdr_${"1".repeat(32)}`;
const FIXED_RELEASE_ID = `sdr_${"2".repeat(32)}`;
const SBOM_ID = `sbr_${"3".repeat(32)}`;
const SIGNATURE_ID = `asi_${"4".repeat(32)}`;
const PROVENANCE_ID = `bpr_${"5".repeat(32)}`;
const FINDING_ID = `vfn_${"6".repeat(32)}`;
const EXCEPTION_ID = `sex_${"7".repeat(32)}`;
const PATCH_ID = `ptr_${"8".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"9".repeat(32)}`;
const API_ID = `apv_${"a".repeat(32)}`;
const ARTIFACT_SHA = "a".repeat(64);
const SBOM_SHA = "b".repeat(64);
const PROVENANCE_SHA = "c".repeat(64);
const FIXED_SHA = "d".repeat(64);
const NOW = "2026-08-31T08:00:00.000Z";
const EXPIRES = "2026-09-07T08:00:00.000Z";
const KEY = await webcrypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const PUBLIC_KEY_B64 = Buffer.from(
  await webcrypto.subtle.exportKey("raw", KEY.publicKey),
).toString("base64url");
const ATTESTATION_PAYLOAD = new TextEncoder().encode(JSON.stringify({
  artifact_sha256: ARTIFACT_SHA,
  provenance_sha256: PROVENANCE_SHA,
  purpose: "metahub.artifact.attestation/v1",
  sbom_sha256: SBOM_SHA,
  sdk_release_id: RELEASE_ID,
}));
const ATTESTATION_SIGNATURE = Buffer.from(
  await webcrypto.subtle.sign({ name: "Ed25519" }, KEY.privateKey, ATTESTATION_PAYLOAD),
);

test("Developer and Platform clients verify the exact attestation chain and isolate credentials", async (t) => {
  installGlobals(t);
  const calls = installFetch(t, (url, options) => routeResponse(new URL(url).pathname, options));

  const developer = await getDeveloperSupplyChainSecurity({
    controlApiUrl: CONTROL_URL,
    token: TOKEN,
    releaseIds: [RELEASE_ID],
  });
  assert.equal(developer[RELEASE_ID].status, "ready");
  assert.equal(developer[RELEASE_ID].report.signature.client_verification_outcome, "passed");
  assert.equal(developer[RELEASE_ID].report.vulnerabilities[0].patch_releases[0].status,
    "released");

  const platform = await getPlatformSupplyChainSecurity({
    controlApiUrl: CONTROL_URL,
    releaseIds: [RELEASE_ID],
  });
  assert.equal(platform[RELEASE_ID].provenance.builder_verified, true);

  calls.slice(0, 2).forEach(({ options }) => {
    assert.equal(options.headers.Authorization, `Bearer ${TOKEN}`);
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal("X-CSRF-Token" in options.headers, false);
  });
  calls.slice(2, 4).forEach(({ options }) => {
    assert.equal(options.headers.Authorization, undefined);
    assert.equal(options.credentials, "include");
  });

  const exception = await applyPlatformSecurityException({
    controlApiUrl: CONTROL_URL,
    findingId: FINDING_ID,
    operation: "request",
    workspaceId: WORKSPACE_ID,
    reasonCode: "vendor_fix_pending",
    compensatingControls: ["runtime_detection", "network_isolation"],
    startsAt: NOW,
    expiresAt: EXPIRES,
    confirmed: true,
    idempotencyKey: IDEMPOTENCY,
  });
  assert.equal(exception.exception.security_exception_id, EXCEPTION_ID);
  const write = calls.at(-1).options;
  assert.equal(write.credentials, "include");
  assert.equal(write.headers.Authorization, undefined);
  assert.equal(write.headers["X-CSRF-Token"], CSRF);
  assert.equal(write.headers["Idempotency-Key"], IDEMPOTENCY);
  assert.deepEqual(JSON.parse(write.body), {
    operation: "request",
    workspace_id: WORKSPACE_ID,
    reason_code: "vendor_fix_pending",
    compensating_controls: ["network_isolation", "runtime_detection"],
    starts_at: NOW,
    expires_at: EXPIRES,
    confirmed: true,
  });
});

test("strict clients reject unknown fields, owner drift, and stale requests", async (t) => {
  installGlobals(t);
  installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/security-report")) {
      return jsonResponse({ ...securityReportResponse(), repository_url: "https://internal" });
    }
    return routeResponse(path, options);
  });
  await assert.rejects(
    () => getDeveloperSupplyChainSecurity({
      controlApiUrl: CONTROL_URL, token: TOKEN, releaseIds: [RELEASE_ID],
    }),
    (error) => error instanceof AuthApiError && error.code === "supply_chain.response_invalid",
  );

  globalThis.fetch = async (url, options) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/provenance")) {
      return jsonResponse({
        ...provenanceResponse(),
        provenance: { ...provenanceResponse().provenance, sdk_release_id: FIXED_RELEASE_ID },
      });
    }
    return routeResponse(path, options);
  };
  await assert.rejects(
    () => getDeveloperSupplyChainSecurity({
      controlApiUrl: CONTROL_URL, token: TOKEN, releaseIds: [RELEASE_ID],
    }),
    (error) => error.code === "supply_chain.response_invalid",
  );

  const controller = new AbortController();
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => {
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
  const pending = getPlatformSupplyChainSecurity({
    controlApiUrl: CONTROL_URL, releaseIds: [RELEASE_ID], signal: controller.signal,
  });
  controller.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError");
});

test("Developer and Platform release views expose gate, SLA, exception, and patch evidence", () => {
  const supplyChain = { [RELEASE_ID]: normalizedEvidence() };
  const developerHtml = renderDeveloperContracts({
    versions: [],
    changelog: { entries: [] },
    schemas: [],
    sdk_releases: [publishedSdk()],
    supply_chain: supplyChain,
  });
  assert.match(developerHtml, /Supply chain evidence/u);
  assert.match(developerHtml, /client passed/u);
  assert.match(developerHtml, /CVE-2026-0136/u);
  assert.match(developerHtml, /cloud\/edge N\/N-1 passed/u);
  assert.match(developerHtml, /data-developer-security-download/u);
  assert.doesNotMatch(developerHtml, /component_name|repository_url|private[_ -]?key/iu);

  const root = { innerHTML: "" };
  renderPlatformWorkspace(root, platformState(platformWorkspace(supplyChain)));
  assert.match(root.innerHTML, /Release security gates/u);
  assert.match(root.innerHTML, /data-platform-supply-chain-exception-form/u);
  assert.match(root.innerHTML, /Independent approval/u);
  assert.match(root.innerHTML, /fixed dddddddddd/u);
  assert.doesNotMatch(root.innerHTML, /signature_b64|public_key_b64|Authorization|Bearer/iu);
});

test("Platform controller fences exception writes with idempotency and dynamic permission checks", async (t) => {
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash: "#releases" },
    history: { replaceState() {} },
  };
  t.after(() => { globalThis.window = originalWindow; });
  let permissions = [
    "platform.developer_contract.read",
    "platform.supply_chain.read",
    "platform.supply_chain.exception",
    "platform.supply_chain.approve",
  ];
  const calls = [];
  const workspace = platformWorkspace({ [RELEASE_ID]: normalizedEvidence() });
  const api = {
    session: async () => ({ context: {
      permissions: [...permissions],
      operator: { display_name: "Security", roles: ["platform_security"] },
    } }),
    navigation: async () => ({
      items: [{ id: "releases", label: "Releases", href: "/platform.html#releases" }],
    }),
    developerContracts: async () => workspace,
    supplyChainException: async (input) => {
      calls.push(input);
      return exceptionResponse();
    },
  };
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api,
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  assert.equal(await controller.mount(), true);
  assert.equal(await controller.applySupplyChainException({
    operation: "request",
    findingId: FINDING_ID,
    workspaceId: WORKSPACE_ID,
    reasonCode: "vendor_fix_pending",
    compensatingControls: ["network_isolation"],
    startsAt: NOW,
    expiresAt: EXPIRES,
    confirmed: true,
  }), true);
  assert.equal(calls[0].idempotencyKey, IDEMPOTENCY);
  assert.ok(calls[0].signal instanceof AbortSignal);

  permissions = ["platform.developer_contract.read", "platform.supply_chain.read"];
  assert.equal(await controller.refresh(), true);
  assert.equal(await controller.applySupplyChainException({
    operation: "request",
    findingId: FINDING_ID,
    workspaceId: WORKSPACE_ID,
    reasonCode: "vendor_fix_pending",
    compensatingControls: ["network_isolation"],
    startsAt: NOW,
    expiresAt: EXPIRES,
    confirmed: true,
  }), false);
  assert.equal(calls.length, 1);
  controller.destroy();
});

test("PRD-136 source keeps fixed APIs, request ownership, responsive layouts, and no secret storage", async () => {
  const [client, main, platformMain, state, developerRender, platformRender, css, platformCss,
    routes, migration] = await Promise.all([
    readFile(new URL("../../src/lib/supplyChainSecurity.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-contracts/renderDeveloperContracts.js",
      import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/renderPlatformPage.js", import.meta.url),
      "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/supply_chain_security/routes.py",
      import.meta.url), "utf8"),
    readFile(new URL("../../../infra/migrations/0128_supply_chain_security_provenance.sql",
      import.meta.url), "utf8"),
  ]);
  assert.match(client, /credentials: domain === "developer" \? "omit" : "include"/u);
  assert.match(client, /Promise\.all[\s\S]*security-report[\s\S]*provenance/u);
  assert.match(client, /signal/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|indexedDB/u);
  assert.match(main, /developerContractSecurityController[\s\S]*refreshId !== refreshSequence/u);
  assert.match(platformMain, /getPlatformReleaseWorkspace[\s\S]*supplyChainException/u);
  assert.match(state, /applySupplyChainException[\s\S]*developerContractWrite/u);
  assert.match(developerRender, /proof_missing[\s\S]*Install blocked/u);
  assert.match(platformRender, /platform\.supply_chain\.exception[\s\S]*platform\.supply_chain\.approve/u);
  assert.match(css, /@media \(max-width: 420px\)[\s\S]*developer-security/u);
  assert.match(platformCss, /@media \(max-width: 600px\)[\s\S]*platform-supply-chain/u);
  assert.match(routes, /GET|@router\.get/u);
  assert.match(routes, /\/releases\/\{release_id\}\/security-report/u);
  assert.match(routes, /\/releases\/\{release_id\}\/provenance/u);
  assert.match(routes, /\/platform\/vulnerabilities\/\{finding_id\}\/exceptions/u);
  assert.doesNotMatch(routes, /feed_url|script|repository_url/u);
  assert.match(migration, /platform_security','platform\.developer_contract\.read/u);
});

function routeResponse(path, options) {
  if (path === `/releases/${RELEASE_ID}/security-report`) {
    return jsonResponse(securityReportResponse());
  }
  if (path === `/releases/${RELEASE_ID}/provenance`) return jsonResponse(provenanceResponse());
  if (path === `/platform/vulnerabilities/${FINDING_ID}/exceptions`
    && options.method === "POST") return jsonResponse(exceptionResponse());
  throw new Error(`unexpected route ${path}`);
}

function securityReportResponse() {
  return traced({ report: report() }, "security");
}

function report() {
  return {
    sdk_release_id: RELEASE_ID,
    language: "python",
    sdk_version: "13.6.0",
    artifact_sha256: ARTIFACT_SHA,
    gate_outcome: "blocked",
    blocking_finding_count: 1,
    sbom: {
      sbom_report_id: SBOM_ID,
      format: "spdx",
      format_version: "2.3",
      component_count: 12,
      sbom_sha256: SBOM_SHA,
      immutable_object_receipt_sha256: "e".repeat(64),
      license_summary: { approved: 12, review_required: 0 },
      vulnerability_summary: { critical: 1, high: 0, medium: 0, low: 0 },
      generated_at: NOW,
    },
    signature: {
      artifact_signature_id: SIGNATURE_ID,
      purpose: "metahub.artifact.attestation/v1",
      algorithm: "ed25519",
      key_id: "supply-chain-test-v1",
      key_version: 1,
      public_key_b64: PUBLIC_KEY_B64,
      artifact_sha256: ARTIFACT_SHA,
      sbom_sha256: SBOM_SHA,
      provenance_sha256: PROVENANCE_SHA,
      attestation_sha256: createHash("sha256").update(ATTESTATION_PAYLOAD).digest("hex"),
      signature_b64: ATTESTATION_SIGNATURE.toString("base64url"),
      signature_sha256: createHash("sha256").update(ATTESTATION_SIGNATURE).digest("hex"),
      verification_outcome: "passed",
      signed_at: NOW,
    },
    vulnerabilities: [{
      vulnerability_finding_id: FINDING_ID,
      advisory_code: "CVE-2026-0136",
      severity: "critical",
      evaluation_generation: 2,
      remediation_state: "fixed_available",
      fixed_version_code: "1.0.1",
      first_seen_at: NOW,
      last_seen_at: NOW,
      sla_deadline_at: EXPIRES,
      exceptions: [publicException()],
      patch_releases: [patchRelease()],
    }],
    generated_at: NOW,
  };
}

function provenanceResponse() {
  return traced({ provenance: {
    build_provenance_id: PROVENANCE_ID,
    sdk_release_id: RELEASE_ID,
    artifact_sha256: ARTIFACT_SHA,
    source_commit_sha256: "1".repeat(64),
    source_tree_sha256: "2".repeat(64),
    dependency_lock_sha256: "3".repeat(64),
    builder_verified: true,
    builder_image_sha256: "4".repeat(64),
    build_invocation_sha256: "5".repeat(64),
    artifact_set_sha256: "6".repeat(64),
    environment_allowlist_sha256: "7".repeat(64),
    provenance_sha256: PROVENANCE_SHA,
    reproducibility_receipt_sha256: "8".repeat(64),
    reproducible: true,
    generated_at: NOW,
  } }, "provenance");
}

function publicException() {
  return {
    security_exception_id: EXCEPTION_ID,
    workspace_id: WORKSPACE_ID,
    reason_code: "vendor_fix_pending",
    status: "pending",
    revision: 1,
    starts_at: NOW,
    expires_at: EXPIRES,
  };
}

function exceptionResponse() {
  return traced({
    exception: {
      ...publicException(),
      vulnerability_finding_id: FINDING_ID,
      affected_artifact_sha256: ARTIFACT_SHA,
      compensating_controls: ["network_isolation"],
      review_generation: 1,
      approved_at: null,
      updated_at: NOW,
    },
    idempotent_replay: false,
  }, "exception");
}

function patchRelease() {
  return {
    patch_release_id: PATCH_ID,
    workspace_id: WORKSPACE_ID,
    vulnerability_finding_id: FINDING_ID,
    affected_artifact_sha256: ARTIFACT_SHA,
    fixed_sdk_release_id: FIXED_RELEASE_ID,
    fixed_artifact_sha256: FIXED_SHA,
    compatibility_matrix: {
      cloud: { n: "passed", n_minus_1: "passed" },
      edge: { n: "passed", n_minus_1: "passed" },
    },
    compatibility_sha256: "f".repeat(64),
    rollback_artifact_sha256: ARTIFACT_SHA,
    status: "released",
    revision: 3,
    released_at: NOW,
    updated_at: NOW,
  };
}

function normalizedEvidence() {
  return {
    status: "ready",
    report: {
      ...report(),
      signature: { ...report().signature, client_verification_outcome: "passed" },
    },
    provenance: provenanceResponse().provenance,
    request_ids: ["req_security", "req_provenance"],
  };
}

function publishedSdk() {
  return {
    sdk_release_id: RELEASE_ID,
    api_version: "13.6.0",
    language: "python",
    sdk_version: "13.6.0",
    runtime_version: ">=3.11",
    contract_sha256: "0".repeat(64),
    artifact_sha256: ARTIFACT_SHA,
    artifact_receipt_sha256: "1".repeat(64),
    file_manifest: { "README.md": "2".repeat(64) },
    smoke_matrix: { contract_versions: ["13.6.0", "N-1"], linux: "passed", windows: "passed" },
    status: "published",
    revision: 2,
    signing_key_id: "developer-contract-test-v1",
    signature_b64: "A".repeat(86),
    signature_sha256: "3".repeat(64),
    public_key_b64: "A".repeat(43),
    download_url: "/developer/sdks/python/13.6.0/download",
    created_at: NOW,
    published_at: NOW,
  };
}

function platformWorkspace(supplyChain) {
  return {
    versions: [{
      api_version_id: API_ID,
      version: "13.6.0",
      registry_revision: 1,
      status: "published",
      revision: 2,
      contract_sha256: "0".repeat(64),
      compatibility_baseline_sha256: null,
      compatibility_outcome: "compatible",
      breaking_reasons: [],
      created_at: NOW,
      published_at: NOW,
      deprecated_at: null,
      sunset_at: null,
    }],
    sdk_releases: [publishedSdk()],
    deprecations: [],
    supply_chain: supplyChain,
    request_id: "req_workspace",
    trace_id: "trc_workspace",
  };
}

function platformState(workspace) {
  return {
    loading: false,
    session: { context: {
      permissions: [
        "platform.developer_contract.read",
        "platform.supply_chain.read",
        "platform.supply_chain.exception",
        "platform.supply_chain.approve",
      ],
      operator: { display_name: "Security", roles: ["platform_security"] },
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
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
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
