/**
 * PRD-076 Browser 验收器的纯合同测试。
 *
 * 本文件不启动服务、Provider 或 Chromium，也不占用固定端口；只验证固定路径、CLI、
 * 回环网络、权限集合、双视口布局、安全证据和 SHA-256 清单合同。
 */

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { PassThrough } from "node:stream";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  PUBLIC_POLICY,
  RESPONSE_CACHE_PERMISSIONS,
  RuntimeHarnessClient,
  SCREENSHOT_MASK_LABELS,
  VIEWPORTS,
  WORK_ROOT,
  assertLayoutMetrics,
  assertSafeEvidence,
  assertStaleConflictFacts,
  assertTagInvalidationFacts,
  browserRequestAllowed,
  buildManifest,
  classifyBrowserConsoleError,
  consoleNetworkFailuresAreCanceled,
  consoleNetworkFailureReason,
  cleanupFailedRunDirectories,
  countLayoutOverlaps,
  countPrivateValueViolations,
  isStaleFetchProtocolFailure,
  normalizeBrowserFailure,
  normalizeLoopbackOrigin,
  normalizePermissionCodes,
  parseArguments,
  permissionRevisionExpression,
  policyBody,
  resolveRunDirectory,
  requiresResponseCachePrivateBoundary,
  safeRunId,
  samePermissionCodes,
  scanRuntimePrivateFiles,
  screenshotFilename,
  trackedRequestCategory,
  waitForTrackedNetworkIdle,
} from "../prd076-browser-acceptance.mjs";


// 该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const SCRIPT_PATH = path.join(ROOT, "scripts", "acceptance", "prd076-browser-acceptance.mjs");
const TEMP_ROOT = path.join(ROOT, ".tmp");


test("run paths remain repository-owned direct children", () => {
  assert.equal(WORK_ROOT, path.join(ROOT, ".tmp", "prd076-browser"));
  assert.equal(ARTIFACT_ROOT, path.join(ROOT, ".artifacts", "prd076-browser"));
  assert.equal(resolveRunDirectory(WORK_ROOT, "unit-browser-076"), path.join(WORK_ROOT, "unit-browser-076"));
  assert.match(safeRunId(), /^run-[A-Za-z0-9._-]+$/);

  for (const value of ["..", "../escape", "nested/run", "nested\\run", "C:\\escape", ".hidden", "ab", "含密钥"] ) {
    assert.throws(() => safeRunId(value), BrowserAcceptanceFailure);
  }
});


test("CLI accepts only one safe run-id and never echoes rejected private input", () => {
  assert.deepEqual(parseArguments(["--run-id", "prd076-browser-unit"]), {
    runId: "prd076-browser-unit",
  });
  for (const argv of [
    ["--run-id"],
    ["--unknown", "value"],
    ["--run-id", "valid-run", "--run-id", "second-run"],
    ["--run-id", "../escape"],
  ]) assert.throws(() => parseArguments(argv), BrowserAcceptanceFailure);

  const privateValue = "C:/private/provider-key-canary";
  const result = spawnSync(process.execPath, [SCRIPT_PATH, "--unknown", privateValue], {
    cwd: ROOT,
    encoding: "utf8",
    timeout: 10000,
    windowsHide: true,
  });
  assert.equal(result.status, 1);
  assert.equal(result.stdout, "");
  assert.equal(
    result.stderr,
    '{"status":"failed","stage":"config.arguments","code":"invalid"}\n',
  );
  assert.equal(result.stderr.includes(privateValue), false);
});


test("foreign browser helper failures retain only fixed stage and code", () => {
  const foreign = Object.assign(new Error("private runtime detail"), {
    name: "BrowserAcceptanceFailure",
    stage: "browser.debugger",
    code: "timeout",
  });
  const normalized = normalizeBrowserFailure(foreign);
  assert.ok(normalized instanceof BrowserAcceptanceFailure);
  assert.equal(normalized.stage, "browser.debugger");
  assert.equal(normalized.code, "timeout");
  assert.equal(normalized.message.includes("private runtime detail"), false);

  const rejected = normalizeBrowserFailure(Object.assign(new Error("private"), {
    name: "BrowserAcceptanceFailure",
    stage: "browser/private",
    code: "timeout",
  }));
  assert.equal(rejected.stage, "browser.acceptance");
  assert.equal(rejected.code, "unexpected");

  for (const stage of [
    "browser.cdp.Fetch.continueRequest",
    "browser.cdp.Fetch.failRequest",
  ]) assert.equal(isStaleFetchProtocolFailure(Object.assign(new Error(), {
    name: "BrowserAcceptanceFailure",
    stage,
    code: "protocol",
  })), true);
  assert.equal(isStaleFetchProtocolFailure(Object.assign(new Error(), {
    name: "BrowserAcceptanceFailure",
    stage: "browser.cdp.Fetch.continueRequest",
    code: "timeout",
  })), false);
  assert.equal(isStaleFetchProtocolFailure(Object.assign(new Error(), {
    name: "BrowserAcceptanceFailure",
    stage: "browser.cdp.Runtime.evaluate",
    code: "protocol",
  })), false);
});


test("browser network is restricted to fixed loopback origins without sensitive query keys", () => {
  const allowed = new Set([
    "http://localhost:5173",
    "http://localhost:8001",
    "http://localhost:8000",
  ]);
  assert.equal(normalizeLoopbackOrigin("http://localhost:5173", 5173), "http://localhost:5173");
  for (const value of [
    "https://localhost:5173/",
    "http://127.0.0.1:5173/",
    "http://user:pass@localhost:5173/",
    "http://localhost:5173/path",
    "http://localhost:8001/",
  ]) assert.throws(() => normalizeLoopbackOrigin(value, 5173), BrowserAcceptanceFailure);

  assert.equal(browserRequestAllowed("http://localhost:5173/#response-cache", allowed), true);
  assert.equal(browserRequestAllowed("http://localhost:8001/cache-policies?limit=50", allowed), true);
  assert.equal(browserRequestAllowed("about:blank", allowed), true);
  for (const value of [
    "https://example.com/cache-policies",
    "http://127.0.0.1:8001/cache-policies",
    "http://localhost:8001/cache-policies?tenant_id=forged",
    "http://localhost:8001/cache-policies?provider_key=private",
    "http://localhost:8000/v1/chat/completions?prompt=private",
  ]) assert.equal(browserRequestAllowed(value, allowed), false);
  for (const value of ["about:blank", "data:text/plain,fixture", "blob:http://localhost:5173/unit"]) {
    assert.equal(trackedRequestCategory(value), "browser_internal");
  }
  assert.equal(trackedRequestCategory("https://example.invalid/unit"), "external");
});


test("permission reload waits for a stable tracked-network idle window", async () => {
  const tracker = { activeRequestIds: new Set(["request-1"]) };
  // 模拟旧后台刷新正常完成；验收器必须在稳定空闲窗口之后才能触发 reload。
  setTimeout(() => tracker.activeRequestIds.delete("request-1"), 10);
  assert.equal(await waitForTrackedNetworkIdle(tracker, "browser.permissions.unit", 500, 20), true);
  await assert.rejects(
    waitForTrackedNetworkIdle(
      { activeRequestIds: new Set(["request-stuck"]) },
      "browser.permissions.unit_timeout",
      20,
      10,
    ),
    BrowserAcceptanceFailure,
  );
});


test("browser console classification trusts status codes without retaining reason phrases", () => {
  for (const message of [
    "Failed to load resource: the server responded with a status of 403",
    "Failed to load resource: the server responded with a status of 403 ()",
    "Failed to load resource: the server responded with a status of 409 (Conflict)",
    "Failed to load resource: the server responded with a status of 409 (本地化文本)",
  ]) assert.equal(classifyBrowserConsoleError(message, "error").kind, "expected_http");
  assert.deepEqual(
    classifyBrowserConsoleError(
      "Failed to load resource: the server responded with a status of 401 (Unauthorized)",
      "error",
    ),
    { kind: "unexpected_http", status: 401 },
  );
  assert.deepEqual(
    classifyBrowserConsoleError("Failed to load resource: net::ERR_ABORTED", "error"),
    { kind: "network", code: "aborted" },
  );
  assert.deepEqual(classifyBrowserConsoleError("private detail", "error"), { kind: "other" });
  assert.deepEqual(classifyBrowserConsoleError("private detail", "warning"), { kind: "ignored" });
  assert.equal(
    consoleNetworkFailuresAreCanceled(["failed"], new Set(["failed"]), new Set()),
    true,
  );
  assert.equal(
    consoleNetworkFailuresAreCanceled(["failed"], new Set(["failed"]), new Set(["failed"])),
    false,
  );
  assert.equal(
    consoleNetworkFailuresAreCanceled(["failed"], new Set(), new Set()),
    false,
  );
  assert.equal(
    consoleNetworkFailuresAreCanceled(["failed", "failed"], new Set(), new Set(), 1),
    true,
  );
  assert.equal(
    consoleNetworkFailuresAreCanceled(["failed", "failed", "failed"], new Set(), new Set(), 1),
    false,
  );
  assert.equal(
    consoleNetworkFailuresAreCanceled(["refused"], new Set(), new Set(), 1),
    false,
  );
  assert.equal(
    consoleNetworkFailuresAreCanceled(["failed"], new Set(), new Set(["failed"]), 1),
    false,
  );
  assert.equal(consoleNetworkFailureReason(["failed"], new Set(), new Set(), 0), "no_stale");
  assert.equal(consoleNetworkFailureReason(["failed", "failed", "failed"], new Set(), new Set(), 1), "excess");
  assert.equal(consoleNetworkFailureReason(["failed"], new Set(), new Set(["failed"]), 1), "uncanceled");
  assert.equal(consoleNetworkFailureReason(["refused"], new Set(), new Set(), 1), "non_failed_category");
  assert.equal(consoleNetworkFailureReason(["failed", "failed"], new Set(), new Set(), 1), "");
});


test("exact private cache boundary applies only to PRD-076 mutation and policy endpoints", () => {
  assert.equal(requiresResponseCachePrivateBoundary("/cache-policies"), true);
  assert.equal(requiresResponseCachePrivateBoundary("/cache/invalidate"), true);
  for (const pathname of [
    "/auth/login",
    "/auth/logout",
    "/permissions",
    "/roles/role_finance/permissions",
    "/request-logs",
    "/cache-policies/extra",
  ]) assert.equal(requiresResponseCachePrivateBoundary(pathname), false);
});


test("response-cache permissions are independent canonical capabilities", () => {
  assert.deepEqual([...RESPONSE_CACHE_PERMISSIONS], [
    "response_cache.read",
    "response_cache.write",
    "response_cache.invalidate",
  ]);
  assert.equal(Object.isFrozen(RESPONSE_CACHE_PERMISSIONS), true);
  assert.deepEqual(normalizePermissionCodes([
    "response_cache.write",
    "request_logs.read",
    "response_cache.read",
  ]), [
    "request_logs.read",
    "response_cache.read",
    "response_cache.write",
  ]);
  assert.equal(samePermissionCodes(["a.read", "b.write"], ["a.read", "b.write"]), true);
  assert.equal(samePermissionCodes(["a.read", "b.write"], ["b.write", "a.read"]), false);
  for (const value of [[], ["response_cache.read", "response_cache.read"], ["Response Cache"], ["x"]]) {
    assert.throws(() => normalizePermissionCodes(value), BrowserAcceptanceFailure);
  }
});


test("permission refresh waits for the exact mirrored browser revision", () => {
  const expression = permissionRevisionExpression(14);
  const matches = new Function("sessionStorage", `return Boolean(${expression});`);
  // 存储桩只返回公开授权 revision，不构造或暴露任何真实 Session Token。
  const storage = (context) => ({
    getItem: (key) => key === "metahub.auth_context" ? context : null,
  });
  assert.equal(matches(storage(JSON.stringify({
    permission_revision: 14,
    user: { permission_revision: 14 },
  }))), true);
  for (const context of [
    JSON.stringify({ permission_revision: 13, user: { permission_revision: 14 } }),
    JSON.stringify({ permission_revision: 14, user: { permission_revision: 13 } }),
    JSON.stringify({ permission_revision: 14, user: {} }),
    "not-json",
    null,
  ]) assert.equal(matches(storage(context)), false);
  for (const revision of [0, -1, 1.5, Number.NaN]) {
    assert.throws(() => permissionRevisionExpression(revision), BrowserAcceptanceFailure);
  }
});


test("policy fixture stays public, tenant-neutral and CAS-bound", () => {
  assert.deepEqual(policyBody("alias_prd076_unit", 2, 903), {
    model_alias_id: "alias_prd076_unit",
    status: "enabled",
    data_class: PUBLIC_POLICY.dataClass,
    single_data_class_confirmed: true,
    ttl_seconds: 903,
    charge_cache_hits: false,
    tags: [PUBLIC_POLICY.tag],
    expected_version: 2,
  });
  assert.equal(Object.isFrozen(PUBLIC_POLICY), true);
  assert.equal("tenant_id" in policyBody("alias_prd076_unit", 2, 903), false);
});


test("stale conflict keeps the local TTL draft while direct save is disabled", () => {
  const facts = {
    conflict: true,
    version: 1,
    ttl_seconds: PUBLIC_POLICY.draftTtl,
    dirty: false,
  };
  assert.equal(assertStaleConflictFacts(facts), facts);
  for (const changed of [
    { conflict: false },
    { version: 2 },
    { ttl_seconds: PUBLIC_POLICY.concurrentTtl },
    { dirty: true },
  ]) assert.throws(() => assertStaleConflictFacts({ ...facts, ...changed }), BrowserAcceptanceFailure);
});


test("request-log screenshot mask uses the renderer's canonical labels", () => {
  assert.deepEqual([...SCREENSHOT_MASK_LABELS], ["request_id", "trace_id", "租户", "用户"]);
  assert.equal(Object.isFrozen(SCREENSHOT_MASK_LABELS), true);
});


test("tag invalidation must match, delete and clear the selected ready count", () => {
  assert.deepEqual(assertTagInvalidationFacts({
    matched_entry_count: 1,
    deleted_entry_count: 1,
    ready_count_after: 0,
  }), {
    matched_entry_count: 1,
    deleted_entry_count: 1,
    ready_count_after: 0,
  });
  for (const changed of [
    { matched_entry_count: 0 },
    { deleted_entry_count: 0 },
    { matched_entry_count: 1, deleted_entry_count: 2 },
    { ready_count_after: 1 },
  ]) assert.throws(() => assertTagInvalidationFacts({
    matched_entry_count: 1,
    deleted_entry_count: 1,
    ready_count_after: 0,
    ...changed,
  }), BrowserAcceptanceFailure);
});


test("desktop and mobile layout gates reject overflow, clipping and overlap", () => {
  for (const viewport of Object.values(VIEWPORTS)) {
    const valid = {
      viewport_width: viewport.width,
      viewport_height: viewport.height,
      document_scroll_width: viewport.width,
      page_scroll_width: viewport.width,
      target_visible: true,
      target_inside_horizontal: true,
      target_horizontal_overflow: 0,
      text_overflow_count: 0,
      text_overflow_categories: [],
      control_outside_horizontal_count: 0,
      overlap_candidate_count: 12,
      overlap_count: 0,
      dialog_count: 0,
      dialog_actions_reachable: true,
    };
    assert.equal(assertLayoutMetrics(valid, viewport), valid);
    for (const [name, value] of [
      ["document_scroll_width", viewport.width + 2],
      ["target_horizontal_overflow", 2],
      ["text_overflow_count", 1],
      ["control_outside_horizontal_count", 1],
      ["overlap_count", 1],
    ]) assert.throws(() => assertLayoutMetrics({ ...valid, [name]: value }, viewport), BrowserAcceptanceFailure);
    assert.throws(
      () => assertLayoutMetrics({
        ...valid,
        text_overflow_count: 1,
        text_overflow_categories: ["editor"],
      }, viewport),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "text_overflow_editor",
    );
    assert.throws(
      () => assertLayoutMetrics({ ...valid, dialog_count: 0 }, viewport, { expectDialog: true }),
      BrowserAcceptanceFailure,
    );
  }
  assert.equal(screenshotFilename("configured-policy", VIEWPORTS.desktop.name), "configured-policy-desktop-1280x720.png");
  assert.equal(screenshotFilename("request-log-hit", VIEWPORTS.mobile.name), "request-log-hit-mobile-390x844.png");
  assert.throws(() => screenshotFilename("../escape", VIEWPORTS.desktop.name), BrowserAcceptanceFailure);
  assert.throws(() => screenshotFilename("configured-policy", "tablet-768x1024"), BrowserAcceptanceFailure);
});


test("full-region geometry rejects sibling overlap but permits ancestor containment", () => {
  const parentAndChild = [
    { left: 0, top: 0, right: 320, bottom: 240, ancestor_indexes: [] },
    { left: 16, top: 16, right: 304, bottom: 80, ancestor_indexes: [0] },
  ];
  assert.equal(countLayoutOverlaps(parentAndChild), 0);

  const overlappingSiblings = [
    ...parentAndChild,
    { left: 16, top: 100, right: 180, bottom: 148, ancestor_indexes: [0] },
    { left: 120, top: 118, right: 300, bottom: 166, ancestor_indexes: [0] },
  ];
  assert.equal(countLayoutOverlaps(overlappingSiblings), 1);
  assert.equal(countLayoutOverlaps([
    { left: 0, top: 0, right: 100, bottom: 40, ancestor_indexes: [] },
    { left: 100, top: 0, right: 200, bottom: 40, ancestor_indexes: [] },
  ]), 0);
  assert.throws(() => countLayoutOverlaps([]), BrowserAcceptanceFailure);
  assert.throws(() => countLayoutOverlaps([
    { left: 0, top: 0, right: 0, bottom: 40, ancestor_indexes: [] },
  ]), BrowserAcceptanceFailure);
});


test("evidence privacy rejects sensitive key variants, private values and non-JSON data", () => {
  assert.deepEqual(assertSafeEvidence({
    status: "passed",
    request_log: { hit_visible: true, cost_pair_count: 4 },
    provider_call_count: 1,
    screenshots: [{
      layout: {
        viewport_width: 390,
        viewport_height: 844,
        document_scroll_width: 390,
        page_scroll_width: 390,
        target_visible: true,
        target_inside_horizontal: true,
        target_horizontal_overflow: 0,
        text_overflow_count: 0,
        text_overflow_categories: [],
        control_outside_horizontal_count: 0,
        overlap_candidate_count: 12,
        overlap_count: 0,
        dialog_count: 0,
        dialog_actions_reachable: true,
      },
    }],
  }), { exact_safe_evidence: true });

  assert.throws(
    () => assertSafeEvidence({ body_scroll_width: 390 }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "forbidden_key_message_data",
  );

  for (const key of [
    "provider_key",
    "providerApiKey",
    "providerKeyValue",
    "virtual-key",
    "promptText",
    "promptSha256",
    "providerResponse",
    "response_body",
    "rawBody",
    "payload",
    "endpointUrl",
    "databasePath",
    "accessToken",
    "accessTokenHash",
    "credentialId",
  ]) assert.throws(() => assertSafeEvidence({ [key]: "masked" }), BrowserAcceptanceFailure);

  assert.throws(() => assertSafeEvidence({ note: "private-canary" }, ["private-canary"]), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ note: "http://localhost:8000/private" }), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ note: "C:\\private\\runtime.sqlite3" }), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ safe: undefined }), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ safe: Number.NaN }), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ safe: Number.POSITIVE_INFINITY }), BrowserAcceptanceFailure);
});


test("runtime privacy catches raw, URL, base64, hex and UTF-16LE canaries without echoing them", async () => {
  const privateValue = "PRD076 private/+token?Aa1!fixture";
  const utf8 = Buffer.from(privateValue, "utf8");
  const encodings = [
    utf8,
    Buffer.from(encodeURIComponent(privateValue), "ascii"),
    Buffer.from(utf8.toString("base64"), "ascii"),
    Buffer.from(utf8.toString("hex"), "ascii"),
    Buffer.from(privateValue, "utf16le"),
  ];
  for (const encoded of encodings) {
    assert.equal(countPrivateValueViolations(encoded, [privateValue]), 1);
  }

  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.stdin = { writable: true, write: () => true, end() { this.writable = false; } };
  const client = new RuntimeHarnessClient(child);
  const splitLeak = encodings.at(-1);
  child.stderr.write(splitLeak.subarray(0, 11));
  client.setPrivateValues([privateValue]);
  child.stderr.write(splitLeak.subarray(11));
  assert.equal(client.processPrivacyViolationCount, 1);
  child.emit("close");
  await client.waitForStdioClose();

  await mkdir(TEMP_ROOT, { recursive: true });
  const sandbox = await mkdtemp(path.join(TEMP_ROOT, "prd076-browser-privacy-contract-"));
  const runtimeDirectory = path.join(sandbox, "runtime");
  try {
    await mkdir(path.join(runtimeDirectory, "logs"), { recursive: true });
    await writeFile(path.join(runtimeDirectory, "trusted.sqlite3"), utf8);
    await writeFile(path.join(runtimeDirectory, "logs", "safe.log"), "public fixture\n", "utf8");
    assert.deepEqual(await scanRuntimePrivateFiles(runtimeDirectory, [privateValue]), {
      filesScanned: 1,
      violationCount: 0,
    });
    await writeFile(path.join(runtimeDirectory, "logs", "leak.log"), Buffer.concat(encodings));
    assert.deepEqual(await scanRuntimePrivateFiles(runtimeDirectory, [privateValue]), {
      filesScanned: 2,
      violationCount: 1,
    });
  } finally {
    await rm(sandbox, { recursive: true, force: true });
  }
});


test("manifest is deterministic, sorted and excludes itself", () => {
  const browserEvidence = Buffer.from('{"status":"passed"}\n', "utf8");
  const screenshot = Buffer.from("synthetic-png-bytes", "ascii");
  const files = new Map([
    ["request-log-hit-mobile-390x844.png", screenshot],
    ["browser-acceptance.json", browserEvidence],
  ]);
  const manifest = buildManifest(files);
  assert.equal(manifest, [
    `${createHash("sha256").update(browserEvidence).digest("hex")}  browser-acceptance.json\n`,
    `${createHash("sha256").update(screenshot).digest("hex")}  request-log-hit-mobile-390x844.png\n`,
  ].join(""));
  assert.equal(manifest.includes("SHA256SUMS.txt"), false);
  assert.throws(() => buildManifest(new Map([["only-one.json", browserEvidence]])), BrowserAcceptanceFailure);
  assert.throws(() => buildManifest(new Map([
    ["../escape.png", screenshot],
    ["browser-acceptance.json", browserEvidence],
  ])), BrowserAcceptanceFailure);
});


test("cleanup failure removes assigned workflow work, browser profile and staging", async () => {
  await mkdir(TEMP_ROOT, { recursive: true });
  const sandbox = await mkdtemp(path.join(TEMP_ROOT, "prd076-browser-cleanup-contract-"));
  const workRoot = path.join(sandbox, "work-root");
  const artifactRoot = path.join(sandbox, "artifact-root");
  const workDirectory = path.join(workRoot, "owned-run");
  const profileDirectory = path.join(workDirectory, "browser", "profile");
  const stagingDirectory = path.join(artifactRoot, ".owned-run.staging");
  const cleanupFailure = new BrowserAcceptanceFailure("cleanup.session.admin", "logout_failed");
  try {
    await mkdir(profileDirectory, { recursive: true });
    await mkdir(stagingDirectory, { recursive: true });
    await writeFile(path.join(profileDirectory, "synthetic-profile-state"), "public fixture\n", "utf8");
    await writeFile(path.join(stagingDirectory, "browser-acceptance.json"), "{}\n", "utf8");

    const result = await cleanupFailedRunDirectories(
      { workDirectory, stagingDirectory },
      Object.freeze({ status: "passed" }),
      cleanupFailure,
      { workRoot, artifactRoot },
    );

    assert.equal(result, cleanupFailure);
    assert.equal(existsSync(workDirectory), false);
    assert.equal(existsSync(profileDirectory), false);
    assert.equal(existsSync(stagingDirectory), false);
  } finally {
    await rm(sandbox, { recursive: true, force: true });
  }
});


test("source contract keeps real UI, dynamic RBAC, nine screenshots and sanitized output", async () => {
  const source = await readFile(SCRIPT_PATH, "utf8");
  for (const marker of [
    "module.prepare_business_database",
    "module.start_service(\"gateway\"",
    "module.start_service(\"control\"",
    "module.start_console",
    "loginThroughConsole",
    "exerciseConflict",
    "exercisePermissionTiers",
    "openHitRequestLog",
    '"chrome-for-testing"',
    '"taskkill.exe"',
    "networkFailedRequestIds",
    "fetchResolutionFailureCount",
    '"[data-response-cache-chain-id]"',
    'document.querySelectorAll("dt")',
    "labelNode.nextElementSibling",
    '/(?:request|trace)_id\\b/i',
    "visible_chain_text_count",
    "all_108_prds_complete: false",
    "screenshots.length === 9",
    "expectedCode: \"auth.permission_denied\"",
  ]) assert.equal(source.includes(marker), true, marker);
  assert.equal(/console\.(?:log|error|warn|info)\s*\(/.test(source), false);
  assert.equal(source.includes("child.stderr.on(\"data\", (chunk) => this.#acceptStderr(chunk));"), true);
  assert.equal(source.includes("runtimePrivacy = await verifyRuntimePrivacy("), true);
  assert.equal(source.includes("runtimeProcessPrivacyViolationCount"), true);
  assert.equal(source.includes("overlap_candidates: overlapCandidates"), true);
  assert.equal(source.includes("overlap_count: countLayoutOverlaps(overlapCandidates)"), true);
  assert.equal(source.includes("private_values=private_values"), true);
  assert.equal(source.includes("process.stderr.write(`${JSON.stringify({ status: \"failed\", stage: failure.stage, code: failure.code })}"), true);
});
