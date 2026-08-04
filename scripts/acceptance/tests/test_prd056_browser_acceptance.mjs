/**
 * PRD-056 Browser 验收器的纯沙箱合同测试。
 *
 * 本文件不启动服务、浏览器或 Playwright；所有临时文件都创建在 D:\\MetaHub\\.tmp，
 * 只验证前置证据、回环网络、隐私、像素、租户限定清理和证据独占发布合同。
 */

import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

import {
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertArtifactDirectorySafe,
  assertBrowserPrivacy,
  assertLoggedOutState,
  assertLayoutMetrics,
  assertSafeEvidence,
  browserRequestAllowed,
  classifyBrowserConsoleMessage,
  createScreenshotDirectory,
  createTemporaryAccountLedger,
  disableTemporaryUsersAndSessions,
  inspectPngPixels,
  normalizeLoopbackOrigin,
  parseCurrentErrorMapVersion,
  planConflictPriorities,
  publishEvidence,
  readEnvironmentIdentity,
  requestLogTargets,
  resolveArtifactDirectory,
  runtimePublicIdentifiers,
  screenshotFilename,
  validateRuntimeEvidence,
} from "../prd056-browser-acceptance.mjs";


// 合同测试根目录固定在工作区内，避免 Node 或 SQLite 回退到系统临时目录。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const SANDBOX_ROOT = path.join(ROOT, ".tmp");
const ERROR_MAP_ID = `pem_${"1".repeat(32)}`;


/** 在工作区 .tmp 下创建唯一普通目录；目录保留为本轮合同测试证据。 */
async function createSandboxDirectory(prefix) {
  await mkdir(SANDBOX_ROOT, { recursive: true });
  return mkdtemp(path.join(SANDBOX_ROOT, prefix));
}


/** 构造 Browser 启动前必须消费的最小完整 Runtime 通过证据。 */
function runtimeEvidence() {
  return {
    schema_version: 1,
    prd_id: "PRD-056",
    status: "passed",
    generated_resources: {
      error_map_id: ERROR_MAP_ID,
      error_map_version: 2,
      head_revision: 2,
      mapped_request_id: "req_p56_mapped_browser_unit",
      nonstream_request_id: "req_p56_nonstream_browser_unit",
      stream_request_id: "req_p56_stream_browser_unit",
    },
    checks: {
      gateway: { hot_update_without_gateway_restart: true },
      cleanup: { all_sessions_revoked: true, temporary_users_disabled: true },
    },
  };
}


/** 构造无外部依赖、尺寸明确且颜色足够丰富的 RGB PNG。 */
function syntheticPng(width = 8, height = 8) {
  /** 生成带长度和 CRC 占位的 PNG 数据块；像素检查器不依赖 CRC。 */
  const chunk = (type, data) => {
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    return Buffer.concat([length, Buffer.from(type, "ascii"), data, Buffer.alloc(4)]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  const rows = [];
  for (let row = 0; row < height; row += 1) {
    rows.push(0);
    for (let column = 0; column < width; column += 1) {
      rows.push((column * 37 + row * 11) & 255, (row * 53 + column * 7) & 255, (column * 19 + row * 29) & 255);
    }
  }
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(Buffer.from(rows))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}


test("loopback, artifact and network boundaries fail closed", async () => {
  assert.equal(normalizeLoopbackOrigin("http://127.0.0.1:15173", "console"), "http://127.0.0.1:15173");
  for (const unsafe of [
    "https://127.0.0.1:15173",
    "http://example.com:15173",
    "http://user:pass@127.0.0.1:15173",
    "http://127.0.0.1:15173/path",
    "http://127.0.0.1",
  ]) assert.throws(() => normalizeLoopbackOrigin(unsafe, "console"), BrowserAcceptanceFailure);

  const expected = path.join(ROOT, ".tmp", "prd056", "unit-batch01");
  assert.equal(resolveArtifactDirectory(".tmp/prd056/unit-batch01", ROOT), expected);
  assert.throws(() => resolveArtifactDirectory(".tmp/prd055/unit-batch01", ROOT), BrowserAcceptanceFailure);
  assert.throws(() => resolveArtifactDirectory(".tmp/prd056/nested/unit-batch01", ROOT), BrowserAcceptanceFailure);

  const allowed = new Set(["http://localhost:5173", "http://localhost:8001", "http://localhost:8000"]);
  assert.equal(browserRequestAllowed("http://localhost:5173/assets/main.js?v=1", allowed), true);
  assert.equal(browserRequestAllowed("http://localhost:8001/error-maps", allowed), true);
  assert.equal(browserRequestAllowed("http://localhost:8001/error-maps?tenant_id=forged", allowed), false);
  assert.equal(browserRequestAllowed("https://example.com/private", allowed), false);

  const batch = await createSandboxDirectory("prd056-browser-path-");
  await writeFile(path.join(batch, "runtime-acceptance.json"), "{}\n", "utf8");
  await writeFile(path.join(batch, "runtime.db"), "", "utf8");
  assert.equal(assertArtifactDirectorySafe(batch), path.resolve(batch));
});


test("browser console classifier allows only Chromium network 403 and 409 noise", () => {
  assert.equal(classifyBrowserConsoleMessage({ type: "log", text: "ignored", argumentCount: 1 }), "ignored");
  for (const status of ["403 (Forbidden)", "409 (Conflict)", "403", "409"]) {
    assert.equal(classifyBrowserConsoleMessage({
      type: "error",
      text: "Failed to load resource: the server responded with a status of " + status,
      argumentCount: 0,
    }), "expected_http_error");
  }
  for (const input of [
    { type: "error", text: "Failed to load resource: the server responded with a status of 403 (Forbidden)", argumentCount: 1 },
    { type: "error", text: "Failed to load resource: the server responded with a status of 401 (Unauthorized)", argumentCount: 0 },
    { type: "error", text: "Failed to load resource: the server responded with a status of 404 (Not Found)", argumentCount: 0 },
    { type: "error", text: "Failed to load resource: the server responded with a status of 500 (Internal Server Error)", argumentCount: 0 },
    { type: "error", text: "Failed to load resource: the server responded with a status of 403 (Unexpected Reason)", argumentCount: 0 },
    { type: "error", text: "Failed to load resource: the server responded with a status of 403 (Forbidden)\nextra", argumentCount: 0 },
    { type: "error", text: "Failed to load resource: the server responded with a status of 409 (Conflict) http://localhost:8001/error-maps", argumentCount: 0 },
    { type: "error", text: "application console failure", argumentCount: 0 },
  ]) assert.equal(classifyBrowserConsoleMessage(input), "unexpected_error");
  assert.equal(classifyBrowserConsoleMessage(), "ignored");
});


test("identity aliases are removed before browser launch", () => {
  const env = {
    METAHUB_PRD056_ADMIN_PASSWORD: "BrowserPrimary!2026",
    METAHUB_ADMIN_PASSWORD: "BrowserFallback!2026",
    METAHUB_PRD056_ADMIN_EMAIL: "Admin@MetaHub.Local",
    METAHUB_PRD056_TENANT_SLUG: "default",
  };
  assert.deepEqual(readEnvironmentIdentity(env), {
    role: "admin",
    email: "admin@metahub.local",
    password: "BrowserPrimary!2026",
    tenantSlug: "default",
  });
  assert.deepEqual(Object.keys(env), []);
});


test("runtime evidence binds mapped Fact and filters numeric revisions", () => {
  const runtime = validateRuntimeEvidence(runtimeEvidence());
  assert.deepEqual(runtime, {
    errorMapId: ERROR_MAP_ID,
    errorMapVersion: 2,
    headRevision: 2,
    mappedRequestId: "req_p56_mapped_browser_unit",
    nonstreamRequestId: "req_p56_nonstream_browser_unit",
    streamRequestId: "req_p56_stream_browser_unit",
  });
  assert.deepEqual(runtimePublicIdentifiers(runtime), [
    ERROR_MAP_ID,
    "req_p56_mapped_browser_unit",
    "req_p56_nonstream_browser_unit",
    "req_p56_stream_browser_unit",
  ]);
  assert.deepEqual(requestLogTargets(runtime), [
    { key: "mapped", requestId: "req_p56_mapped_browser_unit", owner: "attempt", tenantRuleJump: true },
    { key: "stream", requestId: "req_p56_stream_browser_unit", owner: "stream_session", tenantRuleJump: false },
  ]);
  const missingMapped = structuredClone(runtimeEvidence());
  delete missingMapped.generated_resources.mapped_request_id;
  assert.throws(() => validateRuntimeEvidence(missingMapped), BrowserAcceptanceFailure);
  const activeSession = structuredClone(runtimeEvidence());
  activeSession.checks.cleanup.all_sessions_revoked = false;
  assert.throws(() => validateRuntimeEvidence(activeSession), BrowserAcceptanceFailure);
  const activeTemporaryUser = structuredClone(runtimeEvidence());
  activeTemporaryUser.checks.cleanup.temporary_users_disabled = false;
  assert.throws(() => validateRuntimeEvidence(activeTemporaryUser), BrowserAcceptanceFailure);
  assert.throws(() => runtimePublicIdentifiers(null), BrowserAcceptanceFailure);
});


test("current error-map CAS version and conflict priorities remain rerunnable", () => {
  assert.equal(parseCurrentErrorMapVersion("2", 2), 2);
  assert.equal(parseCurrentErrorMapVersion("4", 2), 4);
  for (const invalid of ["1", "2.5", "2e0", "02", " 2 ", "2x", "0", "", null, String(Number.MAX_SAFE_INTEGER + 1)]) {
    assert.throws(() => parseCurrentErrorMapVersion(invalid, 2), BrowserAcceptanceFailure);
  }
  assert.throws(() => parseCurrentErrorMapVersion("2", 2.5), BrowserAcceptanceFailure);

  for (const current of ["1", "911", "912", "913", "914", "1000"]) {
    const plan = planConflictPriorities(current);
    const priorities = [plan.directPriority, plan.stalePriority, plan.finalPriority];
    assert.equal(new Set(priorities).size, 3);
    assert.equal(priorities.includes(Number(current)), false);
    assert.equal(priorities.every((priority) => priority >= 1 && priority <= 1000), true);
  }
  for (const invalid of ["0", "1001", "2.5", "2e0", "01", " 911 ", "", null]) {
    assert.throws(() => planConflictPriorities(invalid), BrowserAcceptanceFailure);
  }
});


test("logout clears auth and protected state without rejecting benign development keys", () => {
  const clean = {
    local_storage_count: 0,
    session_auth_slot_count: 0,
    session_non_auth_slot_count: 2,
    forbidden_storage_key_count: 0,
    protected_dom_count: 0,
  };
  assert.deepEqual(assertLoggedOutState(clean), {
    storage_cleared: true,
    protected_dom_cleared: true,
  });
  const failureCodes = new Map([
    ["local_storage_count", "local_storage_not_cleared"],
    ["session_auth_slot_count", "auth_slots_not_cleared"],
    ["forbidden_storage_key_count", "sensitive_storage_not_cleared"],
    ["protected_dom_count", "protected_dom_not_cleared"],
  ]);
  for (const [name, code] of failureCodes) {
    assert.throws(
      () => assertLoggedOutState({ ...clean, [name]: 1 }),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === code,
    );
  }
  assert.throws(() => assertLoggedOutState({ ...clean, session_non_auth_slot_count: -1 }), BrowserAcceptanceFailure);
  assert.throws(() => assertLoggedOutState(null), BrowserAcceptanceFailure);
});


test("layout, screenshot naming and PNG pixels reject visual regressions", () => {
  const metrics = {
    viewport_width: VIEWPORTS.mobile.width,
    viewport_height: VIEWPORTS.mobile.height,
    document_scroll_width: VIEWPORTS.mobile.width,
    body_scroll_width: VIEWPORTS.mobile.width,
    target_visible: true,
    target_inside_viewport: true,
    navigation_main_overlap_area: 0,
    dialog_count: 1,
  };
  assert.deepEqual(assertLayoutMetrics(metrics, VIEWPORTS.mobile), metrics);
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, document_scroll_width: 500 }, VIEWPORTS.mobile),
    BrowserAcceptanceFailure,
  );
  assert.equal(screenshotFilename("admin-mobile-edit", VIEWPORTS.mobile.name), "admin-mobile-edit-mobile-390x844.png");
  assert.throws(() => screenshotFilename("../escape", VIEWPORTS.mobile.name), BrowserAcceptanceFailure);
  assert.equal(inspectPngPixels(syntheticPng(), { width: 8, height: 8 }).non_blank_pixels, true);
  assert.throws(() => inspectPngPixels(syntheticPng(), { width: 9, height: 8 }), BrowserAcceptanceFailure);
});


test("privacy and exclusive evidence publication reject sensitive or stale output", async () => {
  assert.deepEqual(assertBrowserPrivacy({
    local_storage_count: 0,
    session_auth_slot_count: 2,
    forbidden_storage_key_count: 0,
    public_id_url_or_storage_hits: 0,
    secret_url_dom_storage_hits: 0,
    provider_body_hits: 0,
    bearer_dom_hits: 0,
    auth_token_exposed: false,
  }), { url_dom_storage_sensitive_hits: 0, auth_storage_contract_valid: true });
  assert.deepEqual(assertSafeEvidence({ prd_id: "PRD-056", request_id: "req_public" }), { evidence_privacy_verified: true });
  assert.throws(() => assertSafeEvidence({ password: "plain" }), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ value: "Bearer unit-session-value" }), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ value: "known-private-value" }, ["known-private-value"]), BrowserAcceptanceFailure);

  const batch = await createSandboxDirectory("prd056-browser-evidence-");
  await publishEvidence(batch, { prd_id: "PRD-056", status: "passed" });
  await assert.rejects(
    publishEvidence(batch, { prd_id: "PRD-056", status: "passed" }),
    BrowserAcceptanceFailure,
  );
  const screenshotBatch = await createSandboxDirectory("prd056-browser-screens-");
  assert.equal(await createScreenshotDirectory(screenshotBatch), path.join(screenshotBatch, "screenshots"));
  await assert.rejects(createScreenshotDirectory(screenshotBatch), BrowserAcceptanceFailure);
});


test("temporary account cleanup is tenant qualified and preserves colliding users", async () => {
  const batch = await createSandboxDirectory("prd056-browser-cleanup-");
  const databasePath = path.join(batch, "runtime.db");
  const database = new DatabaseSync(databasePath);
  try {
    database.exec(`
      CREATE TABLE tenants(tenant_id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE);
      CREATE TABLE users(
        user_id TEXT,tenant_id TEXT,email TEXT,display_name TEXT,role TEXT,status TEXT,
        created_at TEXT,updated_at TEXT,PRIMARY KEY(tenant_id,user_id)
      );
      CREATE TABLE sessions(
        session_id TEXT PRIMARY KEY,tenant_id TEXT,user_id TEXT,status TEXT,
        revoked_at TEXT,updated_at TEXT
      );
      INSERT INTO tenants VALUES('tenant-a','default'),('tenant-b','other');
    `);
    const createdAt = new Date().toISOString();
    const insertUser = database.prepare("INSERT INTO users VALUES(?,?,?,?,?,?,?,?)");
    insertUser.run("shared-user", "tenant-a", "prd056-browser-developer-unit@metahub.local", "PRD056 Browser developer unit", "developer", "active", createdAt, createdAt);
    insertUser.run("shared-user", "tenant-b", "other@metahub.local", "Other tenant", "developer", "active", createdAt, createdAt);
    const insertSession = database.prepare("INSERT INTO sessions VALUES(?,?,?,?,?,?)");
    insertSession.run("session-a", "tenant-a", "shared-user", "active", null, createdAt);
    insertSession.run("session-b", "tenant-b", "shared-user", "active", null, createdAt);
  } finally {
    database.close();
  }

  const config = { tenantSlug: "default", databasePath };
  const ledger = createTemporaryAccountLedger(config);
  ledger.intents.push({
    role: "developer",
    email: "prd056-browser-developer-unit@metahub.local",
    displayName: "PRD056 Browser developer unit",
    userId: "shared-user",
    tenantId: "tenant-a",
  });
  const result = disableTemporaryUsersAndSessions(config, ledger);
  assert.equal(result.all_matched_users_disabled, true);
  assert.equal(result.all_matched_sessions_revoked, true);

  const verified = new DatabaseSync(databasePath, { readOnly: true });
  try {
    assert.equal(verified.prepare("SELECT status FROM users WHERE tenant_id='tenant-a'").get().status, "disabled");
    assert.equal(verified.prepare("SELECT status FROM users WHERE tenant_id='tenant-b'").get().status, "active");
    assert.equal(verified.prepare("SELECT status FROM sessions WHERE session_id='session-a'").get().status, "revoked");
    assert.equal(verified.prepare("SELECT status FROM sessions WHERE session_id='session-b'").get().status, "active");
  } finally {
    verified.close();
  }
});


test("logout response waiter is joined with the click and cannot reject unhandled", async () => {
  const source = await readFile(new URL("../prd056-browser-acceptance.mjs", import.meta.url), "utf8");
  assert.match(
    source,
    /async function logoutAndVerify[\s\S]*Promise\.all\([\s\S]*page\.waitForResponse[\s\S]*#logout-button/,
  );
  assert.doesNotMatch(
    source,
    /const responsePromise = page\.waitForResponse[\s\S]*await page\.locator\("#logout-button"\)\.click\(\)[\s\S]*await responsePromise/,
  );
});
