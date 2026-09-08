import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  bindConsoleWorkspaceContextInvalidation,
  closeConsoleWorkspaceContextInvalidation,
  isConsoleWorkspaceContextInvalidation,
} from "../../src/app/main.js";
import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import {
  edgeInstallerArtifactUrl,
  listEdgeInstallers,
} from "../../src/lib/edgeManagement.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const TENANT_ID = "tenant_edge_prd124";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const RELEASE_ID = `irl_${"3".repeat(32)}`;
const STANDARD_PROFILE_ID = `ipr_${"4".repeat(32)}`;
const COMPACT_PROFILE_ID = `ipr_${"5".repeat(32)}`;
const PREFLIGHT_ID = `pfr_${"6".repeat(32)}`;
const INSTALLATION_ID = `ins_${"7".repeat(32)}`;
const CHECK_CODES = [
  "artifact.integrity", "host.os", "host.architecture", "host.root", "host.cpu",
  "host.memory", "host.disk", "network.ports", "network.dns", "host.clock",
  "network.proxy", "network.ca", "host.filesystem", "runtime.container",
];
const ALL = [
  PERMISSIONS.EDGE_DEPLOYMENT_READ,
  PERMISSIONS.EDGE_DEPLOYMENT_MANAGE,
  PERMISSIONS.EDGE_INSTALLER_READ,
  PERMISSIONS.EDGE_INSTALLATION_MANAGE,
];

function context(permissionCodes = ALL, fingerprint = "edge_prd124_authz") {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, name: "PRD-124 tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: "usr_edge_prd124",
      role: "owner",
      email: "edge-prd124@example.test",
    },
    session: { session_id: "ses_edge_prd124", session_revision: 1 },
  }, permissionCodes, {
    permissionRevision: 34,
    authorizationFingerprint: fingerprint,
  });
}

function shell(auth = context()) {
  return {
    state: "ok",
    activeNavigationItemId: "private-gateways",
    navigation: {
      active_navigation_item_id: "private-gateways",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [{
        id: "private-gateways",
        target_section: "private-gateways",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.EDGE_DEPLOYMENT_READ],
      }],
    },
  };
}

function deployment(overrides = {}) {
  return {
    deployment_id: DEPLOYMENT_ID,
    workspace_id: WORKSPACE_ID,
    display_name: "PRD-124 private gateway",
    region_code: "cn-east-1",
    mode: "connected",
    status: "active",
    revision: 2,
    revocation_generation: 1,
    agent_version: "1.0.0",
    health_code: "offline",
    last_connected_at: null,
    certificate_expires_at: null,
    identity_generation: null,
    current_certificate: null,
    recent_events: [],
    created_at: "2026-08-27T03:00:00Z",
    updated_at: "2026-08-27T03:08:00Z",
    revoked_at: null,
    ...overrides,
  };
}

function profile(id, code, resources) {
  return {
    install_profile_id: id,
    profile_code: code,
    display_name: `${code === "standard" ? "Standard" : "Compact"} private gateway`,
    cpu_cores: resources.cpu,
    memory_mib: resources.memory,
    disk_mib: resources.disk,
    required_ports: [443, 8000, 8090],
    dns_targets: ["api.metahub.example", "registry.metahub.example"],
    runtime: "docker",
    runtime_min_version: "24.0.0",
    filesystem: "local",
    proxy_modes: ["direct", "http_connect"],
    ca_mode: "system_or_custom",
  };
}

function artifact(index, kind, filename, mediaType, executable = false) {
  return {
    artifact_id: `ira_${String(index).repeat(32)}`,
    kind,
    filename,
    media_type: mediaType,
    sha256: String(index).repeat(64),
    sbom_sha256: "5".repeat(64),
    size_bytes: 1_000 + index,
    executable,
  };
}

function release() {
  return {
    manifest: {
      schema_version: "metahub.installer/v1",
      release_id: RELEASE_ID,
      version: "1.0.0",
      channel: "stable",
      signature_purpose: "metahub.edge-installer.release/v1",
      supported_os: ["linux"],
      supported_arch: ["aarch64", "x86_64"],
      schema_min: 115,
      schema_max: 116,
      agent_min_version: "1.0.0",
      agent_max_version: "1.1.99",
      control_min_version: "1.0.0",
      control_max_version: "1.1.99",
      n_minus_one: true,
      artifacts: [
        artifact(1, "cli", "metahub-installer.py", "text/x-python", true),
        artifact(2, "compose", "compose.yaml", "application/yaml"),
        artifact(3, "container_bundle", "container-images.json", "application/vnd.docker.distribution.manifest.v2+json"),
        artifact(4, "profile", "profiles.json", "application/json"),
        artifact(5, "sbom", "sbom.spdx.json", "application/spdx+json"),
      ],
      profiles: [
        profile(COMPACT_PROFILE_ID, "compact", { cpu: 2, memory: 2_048, disk: 10_240 }),
        profile(STANDARD_PROFILE_ID, "standard", { cpu: 4, memory: 8_192, disk: 40_960 }),
      ],
    },
    manifest_sha256: "f".repeat(64),
    signature: {
      algorithm: "Ed25519",
      key_id: "metahub-installer-release-v1",
      public_key: "A".repeat(43),
      value: "B".repeat(86),
    },
  };
}

function preflight(overrides = {}) {
  return {
    preflight_report_id: PREFLIGHT_ID,
    deployment_id: DEPLOYMENT_ID,
    release_id: RELEASE_ID,
    install_profile_id: STANDARD_PROFILE_ID,
    generation: 1,
    outcome: "failed",
    checks: CHECK_CODES.map((code) => ({
      code,
      outcome: code === "host.disk" ? "failed" : "passed",
      required: true,
      reason_code: code === "host.disk" ? "disk.insufficient" : "ok",
      duration_ms: 100,
    })),
    duration_ms: 1_400,
    started_at: "2026-08-27T03:05:00.000Z",
    completed_at: "2026-08-27T03:05:01.400Z",
    received_at: "2026-08-27T03:05:02.000Z",
    ...overrides,
  };
}

function installation(overrides = {}) {
  return {
    installation_id: INSTALLATION_ID,
    deployment_id: DEPLOYMENT_ID,
    release_id: RELEASE_ID,
    install_profile_id: STANDARD_PROFILE_ID,
    preflight_report_id: PREFLIGHT_ID,
    generation: 1,
    status: "failed",
    current_step: "runtime_validated",
    revision: 3,
    journal_sha256: "8".repeat(64),
    last_receipt_sha256: null,
    compensation_status: "completed",
    retain_config: true,
    retain_audit: true,
    retain_secrets: true,
    failure_reason_code: "runtime.start_failed",
    started_at: "2026-08-27T03:06:00Z",
    updated_at: "2026-08-27T03:08:00Z",
    completed_at: null,
    failed_at: "2026-08-27T03:08:00Z",
    ...overrides,
  };
}

function catalog(overrides = {}) {
  const item = release();
  return {
    releases: [item],
    downloads: item.manifest.artifacts.map((entry) => ({
      artifact_id: entry.artifact_id,
      release_id: RELEASE_ID,
      filename: entry.filename,
      download_path: `/edge/installers/${RELEASE_ID}/artifacts/${entry.artifact_id}`,
      sha256: entry.sha256,
      size_bytes: entry.size_bytes,
    })),
    preflights: [preflight()],
    installations: [installation()],
    request_id: "req_prd124_catalog",
    trace_id: "trc_prd124_catalog",
    ...overrides,
  };
}

function desiredEntry() {
  return {
    deploymentId: DEPLOYMENT_ID,
    response: {
      state: null,
      revision: 0,
      idempotent_replay: false,
      request_id: "req_prd124_desired",
      trace_id: "trc_prd124_desired",
    },
  };
}

function response(payload) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
    },
  });
}

test("installer catalog client enforces exact safe DTOs and canonical download paths", async (t) => {
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  t.after(() => {
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });
  const calls = [];
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=must-not-be-read" };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return response(catalog());
  };
  const result = await listEdgeInstallers({ controlApiUrl: CONTROL_URL });
  assert.equal(result.releases[0].manifest.profiles.length, 2);
  assert.equal(result.preflights[0].checks[6].code, "host.disk");
  assert.equal(calls[0].url, `${CONTROL_URL}/edge/installers`);
  assert.equal(calls[0].options.credentials, "include");
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);
  assert.equal(
    edgeInstallerArtifactUrl({
      controlApiUrl: CONTROL_URL,
      downloadPath: result.downloads[0].download_path,
    }),
    `${CONTROL_URL}${result.downloads[0].download_path}`,
  );
  assert.throws(
    () => edgeInstallerArtifactUrl({
      controlApiUrl: CONTROL_URL,
      downloadPath: "//attacker.example/installer",
    }),
    (error) => error.code === "edge.request_invalid",
  );

  globalThis.fetch = async () => response(catalog({
    preflights: [preflight({ checks: preflight().checks.map((check, index) => (
      index === 0 ? { ...check, hostname: "must-not-leak" } : check
    )) })],
  }));
  await assert.rejects(
    listEdgeInstallers({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "edge.response_invalid",
  );
  const drifted = catalog();
  drifted.downloads[0] = { ...drifted.downloads[0], download_path: "/edge/installers/other" };
  globalThis.fetch = async () => response(drifted);
  await assert.rejects(
    listEdgeInstallers({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "edge.response_invalid",
  );
});

test("owner state renders environment, preflight, installation, health, and recovery safely", () => {
  const auth = context();
  const owner = privateGatewayPageOwner(auth, shell(auth));
  assert.equal(owner.canReadInstallers, true);
  assert.equal(owner.canManageInstallations, true);
  const state = createPrivateGatewayPageState();
  assert.equal(state.enter(owner), true);
  const load = state.beginLoad(owner, 1);
  assert.equal(state.acceptLoad(
    load,
    { deployments: [deployment()] },
    owner,
    1,
    [desiredEntry()],
    [],
    null,
    catalog(),
  ), true);
  state.finishLoad(load);
  assert.equal(state.snapshot().installerSelection.profileId, STANDARD_PROFILE_ID);
  assert.equal(state.snapshot().installerSelection.architecture, "x86_64");
  let html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /安装与接入[\s\S]*预检结果[\s\S]*磁盘[\s\S]*失败/u);
  assert.match(html, /安装失败[\s\S]*runtime\.start_failed[\s\S]*离线/u);
  assert.match(html, /重新下载恢复介质/u);
  assert.doesNotMatch(html, /public_key|signature\.value|password|Authorization/u);
  assert.doesNotMatch(html, /metahub\s+(?:install|connect).*token/iu);

  assert.equal(state.selectInstallerEnvironment(owner, {
    deploymentId: DEPLOYMENT_ID,
    releaseId: RELEASE_ID,
    profileId: COMPACT_PROFILE_ID,
    architecture: "aarch64",
  }), true);
  assert.equal(state.snapshot().installerSelection.profileId, COMPACT_PROFILE_ID);
  html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /Compact private gateway[\s\S]*aarch64/u);

  const crossTenantCatalog = catalog({
    preflights: [preflight({ deployment_id: `edp_${"9".repeat(32)}` })],
  });
  const refresh = state.beginLoad(owner, 2);
  assert.equal(state.acceptLoad(
    refresh,
    { deployments: [deployment()] },
    owner,
    2,
    [desiredEntry()],
    [],
    null,
    crossTenantCatalog,
  ), false);
  state.finishLoad(refresh);

  const revokedAuth = context([
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_INSTALLER_READ,
  ], "edge_prd124_revoked");
  const revokedOwner = privateGatewayPageOwner(revokedAuth, shell(revokedAuth));
  assert.equal(revokedOwner.canManageInstallations, false);
  assert.equal(state.enter(revokedOwner), true);
  assert.equal(state.snapshot().installerCatalog.releases.length, 0);
});

test("installer wiring stays Cookie-only, parallel, responsive, and credential-free", async () => {
  const [
    clientSource,
    stateSource,
    renderSource,
    bindingSource,
    mainSource,
    cssSource,
    edgePermissionsSource,
  ] = await Promise.all([
    readFile(new URL("../../src/lib/edgeManagement.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/privateGatewayPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/renderPrivateGateways.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/bindPrivateGateways.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/workspace_access/edge_permissions.py", import.meta.url), "utf8"),
  ]);
  assert.match(clientSource, /request\(controlApiUrl, "\/edge\/installers", \{ signal \}, "workspace"\)/u);
  assert.match(mainSource, /Promise\.all\(\[[\s\S]*listEdgeDeployments[\s\S]*listEdgeInstallers/u);
  assert.match(bindingSource, /data-private-gateway-installer-environment/u);
  assert.match(cssSource, /@media \(max-width: 390px\)[\s\S]*private-gateway-installer-steps/u);
  assert.match(edgePermissionsSource, /"edge\.installation\.manage"[\s\S]*"edge\.installer\.read"/u);
  assert.doesNotMatch(stateSource, /handoffCredential|secretBytes/iu);
  assert.doesNotMatch(renderSource, /metahub\s+(?:preflight|install|connect|uninstall)\s+--/iu);
  assert.doesNotMatch(bindingSource, /localStorage|sessionStorage|indexedDB/iu);
});

test("Workspace invalidation synchronously fences stale Edge state before reload", async () => {
  assert.equal(isConsoleWorkspaceContextInvalidation({
    type: "context-invalidated",
    generation: 1,
  }), true);
  for (const message of [
    null,
    [],
    { type: "context-invalidated", generation: 0 },
    { type: "context-invalidated", generation: 1, workspace_id: WORKSPACE_ID },
    { type: "other", generation: 1 },
  ]) assert.equal(isConsoleWorkspaceContextInvalidation(message), false);

  let callbackCount = 0;
  let channelName = "";
  let closed = false;
  const channel = { onmessage: null, close() { closed = true; } };
  try {
    assert.equal(bindConsoleWorkspaceContextInvalidation({
      channelFactory(name) {
        channelName = name;
        return channel;
      },
      onInvalidated() { callbackCount += 1; },
    }), true);
    channel.onmessage({ data: { type: "context-invalidated", generation: 0 } });
    channel.onmessage({ data: { type: "context-invalidated", generation: 2 } });
    assert.equal(channelName, "metahub-workspace-context-v1");
    assert.equal(callbackCount, 1);
  } finally {
    closeConsoleWorkspaceContextInvalidation();
  }
  assert.equal(closed, true);
  assert.equal(channel.onmessage, null);

  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const start = source.indexOf("function handleConsoleWorkspaceContextInvalidation()");
  const end = source.indexOf("function closeConsoleWorkspaceContextInvalidation()", start);
  assert.ok(start >= 0 && end > start);
  const handler = source.slice(start, end);
  const generationIndex = handler.indexOf("refreshSequence += 1;");
  const clearIndex = handler.indexOf("clearConsoleSessionState();");
  const refreshIndex = handler.indexOf("void refresh();");
  assert.ok(generationIndex >= 0 && generationIndex < clearIndex && clearIndex < refreshIndex);
  const cleanupStart = source.indexOf("function clearConsoleSessionState(");
  const cleanupEnd = source.indexOf("function invitationOwnerInput(", cleanupStart);
  assert.match(source.slice(cleanupStart, cleanupEnd), /privateGatewayPageState\.clear\(\)/);
  assert.match(source, /channelFactory\("metahub-workspace-context-v1"\)/u);
  assert.match(
    source,
    /window\.addEventListener\("pagehide", \(\) => \{\s*clearBasicResourceSessionState\(\{ clearDom: true \}\);\s*refreshSequence \+= 1;\s*authSession = \{ context: null, owner: null \};\s*closeConsoleWorkspaceContextInvalidation\(\);/u,
  );
});
