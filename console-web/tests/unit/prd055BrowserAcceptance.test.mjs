/**
 * PRD-055 Browser 验收器纯合同测试。
 * 测试不启动服务或 Chrome；文件写入只使用调用方指定的 D:\MetaHub\.tmp 子目录。
 */
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { deflateSync } from "node:zlib";

import {
  BrowserAcceptanceFailure,
  assertArtifactDirectorySafe,
  assertLayoutMetrics,
  assertNoViewerPrefetch,
  assertSafeEvidence,
  browserRequestAllowed,
  createScreenshotDirectory,
  createTemporaryAccountLedger,
  disableTemporaryUsersAndSessions,
  inspectPngPixels,
  normalizeLoopbackOrigin,
  publishEvidence,
  resolveArtifactDirectory,
  resolveRuntimeDatabasePath,
  runRevocableSession,
  screenshotFilename,
  validateRequestLogList,
  validateRuntimeEvidenceStrict,
} from "../../../scripts/acceptance/prd055-browser-acceptance.mjs";


/** 构造 Browser 必须接受的完整 Runtime 前置证据，不包含认证材料或筛选值。 */
function runtimeEvidenceFixture() {
  return {
    schema_version: 1,
    prd_id: "PRD-055",
    status: "passed",
    generated_resources: {
      shared_request_id: "req_prd055_shared_contract",
      performance_rows: 100000,
    },
    checks: {
      database: {
        migration_0045_applied: true,
        required_indexes_present: true,
        required_triggers_present: true,
        integrity_verified: true,
      },
      services: {
        gateway_http_200: true,
        control_http_200: true,
        console_http_200: true,
      },
      roles: {
        developer_session_bound: true,
        viewer_session_bound: true,
      },
      queries: {
        inline_v1_accurate: true,
        mixed_sources_rejected: true,
        pagination_bounded: true,
        cross_tenant_same_id_isolated: true,
      },
      saved_filters: {
        create_list_apply_update_delete: true,
        owner_isolation_same_tenant: true,
        tenant_isolation: true,
        optimistic_lock: true,
        soft_delete: true,
      },
      query_plans: {
        explain_query_plan_verified: true,
        full_table_scan_rejected: true,
      },
      performance: {
        thresholds_passed: true,
      },
      events: {
        filter_values_absent: true,
        source_outbox_dispatched: true,
        immutable_facts_verified: true,
      },
      final_database: {
        integrity_check: "ok",
        foreign_key_check: "ok",
      },
      cleanup: {
        session_revoked: true,
      },
    },
  };
}


/** 构造最小成功列表，冻结分页、QueryEvent 和请求行的类型合同。 */
function requestLogListFixture() {
  return {
    request_id: "req_operation_contract",
    trace_id: "trc_operation_contract",
    query_event_id: "lqe_" + "1".repeat(32),
    request_logs: [
      {
        request_id: "req_prd055_shared_contract",
        tenant_id: "tenant_contract",
        status: "failed",
        provider_code: "deepseek",
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
    has_more: false,
  };
}


/** 构造最小 PNG chunk；被测解析器不依赖 CRC，测试使用固定零值。 */
function pngChunk(type, data) {
  const chunk = Buffer.alloc(12 + data.length);
  chunk.writeUInt32BE(data.length, 0);
  chunk.write(type, 4, 4, "ascii");
  data.copy(chunk, 8);
  return chunk;
}


/** 构造 2x2 RGBA PNG，可切换多色实像素或单色占位图。 */
function pngFixture(varied = true) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(2, 0);
  header.writeUInt32BE(2, 4);
  header[8] = 8;
  header[9] = 6;
  const white = [255, 255, 255, 255];
  const first = Buffer.from([0, ...white, ...(varied ? [255, 0, 0, 255] : white)]);
  const second = Buffer.from([0, ...(varied ? [0, 0, 255, 255] : white), ...(varied ? [0, 255, 0, 255] : white)]);
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk("IHDR", header),
    pngChunk("IDAT", deflateSync(Buffer.concat([first, second]))),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}


test("PRD-055 artifact path and browser network stay inside explicit loopback boundaries", () => {
  const root = process.cwd();
  const batch = path.join(root, "artifacts", "prd055", "batch-contract-001");
  assert.equal(resolveArtifactDirectory(batch, root), batch);
  assert.throws(
    () => resolveArtifactDirectory(path.join(batch, "nested"), root),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "batch_depth",
  );
  assert.equal(normalizeLoopbackOrigin("http://127.0.0.1:5173", "console"), "http://127.0.0.1:5173");
  assert.throws(
    () => normalizeLoopbackOrigin("https://example.com:5173", "console"),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "scheme_not_http",
  );
  const allowed = new Set(["http://127.0.0.1:5173", "http://127.0.0.1:8001"]);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/request-logs?filters=abc", allowed), true);
  assert.equal(browserRequestAllowed("https://example.com/request-logs", allowed), false);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/request-logs?token=x", allowed), false);
});


test("PRD-055 physical artifact directory rejects missing Runtime evidence", async () => {
  const tempRoot = path.resolve(".tmp");
  await mkdir(tempRoot, { recursive: true });
  const suite = await mkdtemp(path.join(tempRoot, "prd055-artifact-contract-"));
  const root = path.join(suite, "MetaHub");
  const batch = path.join(root, "artifacts", "prd055", "batch-physical-001");
  await mkdir(batch, { recursive: true });
  const runtimeTarget = path.join(batch, "runtime-acceptance.json");
  try {
    await writeFile(runtimeTarget, "{}\n", "utf8");
    assert.equal(assertArtifactDirectorySafe(batch), batch);
    await rm(runtimeTarget, { force: true });
    assert.throws(
      () => assertArtifactDirectorySafe(batch),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "missing_or_unreadable",
    );
  } finally {
    await rm(suite, { recursive: true, force: true });
  }
});


test("PRD-055 Runtime evidence fails closed when performance, RBAC or privacy drifts", () => {
  const evidence = runtimeEvidenceFixture();
  assert.equal(validateRuntimeEvidenceStrict(evidence).sharedRequestId, "req_prd055_shared_contract");

  const missingRole = structuredClone(evidence);
  missingRole.checks.roles.viewer_session_bound = false;
  assert.throws(
    () => validateRuntimeEvidenceStrict(missingRole),
    (error) => error instanceof BrowserAcceptanceFailure && error.stage === "runtime_evidence.roles",
  );

  const unsafe = structuredClone(evidence);
  unsafe.generated_resources.prompt = "forbidden";
  assert.throws(
    () => validateRuntimeEvidenceStrict(unsafe),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "forbidden_key",
  );
});


test("PRD-055 RequestLog DTO requires bounded pagination and a valid QueryEvent", () => {
  const payload = requestLogListFixture();
  assert.deepEqual(
    validateRequestLogList(payload, { expectedRequestId: "req_prd055_shared_contract" }),
    {
      returned_count: 1,
      total: 1,
      limit: 20,
      offset: 0,
      query_event_id_valid: true,
    },
  );
  const invalid = structuredClone(payload);
  invalid.query_event_id = "lqe_short";
  assert.throws(
    () => validateRequestLogList(invalid),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "query_event_id",
  );
  const unsafe = structuredClone(payload);
  unsafe.request_logs[0].response_body = "private";
  assert.throws(
    () => validateRequestLogList(unsafe),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "forbidden_key",
  );
});


test("PRD-055 layout contract rejects overflow, clipping and control overlap", () => {
  const viewport = { width: 1280, height: 720 };
  const metrics = {
    viewport_width: 1280,
    viewport_height: 720,
    document_scroll_width: 1280,
    body_scroll_width: 1280,
    root_left: 200,
    root_right: 1260,
    root_client_width: 1060,
    root_scroll_width: 1060,
    clipped_control_count: 0,
    clipped_content_count: 0,
    horizontal_container_overflow_count: 0,
    control_overlap_count: 0,
  };
  assert.equal(assertLayoutMetrics(metrics, viewport).horizontal_overflow, false);
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, clipped_control_count: 1 }, viewport),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "clipping",
  );
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, control_overlap_count: 1 }, viewport),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "overlap_or_container",
  );
  assert.equal(screenshotFilename("success", "desktop-1280x720"), "screenshots/success-desktop-1280x720.png");
  assert.equal(
    screenshotFilename("saved-filter", "mobile-390x844"),
    "screenshots/saved-filter-mobile-390x844.png",
  );
});


test("PRD-055 PNG contract verifies exact dimensions and nonblank pixels", () => {
  const inspected = inspectPngPixels(pngFixture(true), { width: 2, height: 2 });
  assert.equal(inspected.non_blank_pixels, true);
  assert.match(inspected.sha256, /^[a-f0-9]{64}$/);
  assert.throws(
    () => inspectPngPixels(pngFixture(true), { width: 3, height: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "png_dimensions",
  );
  assert.throws(
    () => inspectPngPixels(pngFixture(false), { width: 2, height: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "png_blank",
  );
});


test("PRD-055 Browser evidence is private and write-once inside workspace temp", async () => {
  const tempRoot = path.resolve(".tmp");
  await mkdir(tempRoot, { recursive: true });
  const directory = await mkdtemp(path.join(tempRoot, "prd055-browser-contract-"));
  const evidence = {
    schema_version: 1,
    prd_id: "PRD-055",
    status: "passed",
    checks: { all_non_blank: true },
  };
  try {
    assertSafeEvidence(evidence);
    assert.equal(await createScreenshotDirectory(directory), path.join(directory, "screenshots"));
    await assert.rejects(
      () => createScreenshotDirectory(directory),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "exists_or_unwritable",
    );
    const target = await publishEvidence(directory, evidence);
    assert.equal(JSON.parse(await readFile(target, "utf8")).status, "passed");
    await assert.rejects(
      () => publishEvidence(directory, evidence),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "write_failed",
    );
    assert.throws(
      () => assertSafeEvidence({ password: "plain" }),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "forbidden_key",
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});



test("PRD-055 temporary browser accounts are disabled without breaking immutable evidence", async () => {
  const tempRoot = path.resolve(".tmp");
  await mkdir(tempRoot, { recursive: true });
  const directory = await mkdtemp(path.join(tempRoot, "prd055-browser-cleanup-"));
  const databasePath = path.join(directory, "runtime.db");
  const createdAt = "2026-07-29T00:00:00.000Z";
  const intents = [
    {
      role: "developer",
      email: "prd055-browser-developer-contract01@metahub.local",
      displayName: "PRD055 Browser developer contract01",
      userId: "usr_prd055_browser_developer_contract",
      tenantId: "tenant_default",
    },
    {
      role: "viewer",
      email: "prd055-browser-viewer-contract02@metahub.local",
      displayName: "PRD055 Browser viewer contract02",
      userId: "usr_prd055_browser_viewer_contract",
      tenantId: "tenant_default",
    },
  ];
  let database = new DatabaseSync(databasePath);
  try {
    database.exec(`
      PRAGMA foreign_keys=ON;
      CREATE TABLE tenants (
        tenant_id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE
      );
      CREATE TABLE users (
        user_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        email TEXT NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
        UNIQUE (tenant_id, email)
      );
      CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL,
        revoked_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
        FOREIGN KEY (user_id) REFERENCES users(user_id)
      );
      CREATE TABLE request_log_query_events (
        query_event_id TEXT PRIMARY KEY,
        actor_user_id TEXT NOT NULL,
        actor_session_id TEXT NOT NULL,
        FOREIGN KEY (actor_user_id) REFERENCES users(user_id),
        FOREIGN KEY (actor_session_id) REFERENCES sessions(session_id)
      );
    `);
    database.prepare("INSERT INTO tenants(tenant_id,slug) VALUES (?,?)")
      .run("tenant_default", "default");
    const insertUser = database.prepare(`
      INSERT INTO users(
        user_id,tenant_id,email,display_name,role,status,created_at,updated_at
      ) VALUES (?,?,?,?,?,?,?,?)
    `);
    insertUser.run(
      "usr_admin_contract",
      "tenant_default",
      "admin-contract@metahub.local",
      "Contract Admin",
      "admin",
      "active",
      createdAt,
      createdAt,
    );
    for (const intent of intents) {
      insertUser.run(
        intent.userId,
        intent.tenantId,
        intent.email,
        intent.displayName,
        intent.role,
        "active",
        createdAt,
        createdAt,
      );
    }
    const insertSession = database.prepare(`
      INSERT INTO sessions(session_id,tenant_id,user_id,status,revoked_at,updated_at)
      VALUES (?,?,?,?,?,?)
    `);
    insertSession.run("ses_admin_contract", "tenant_default", "usr_admin_contract", "active", null, createdAt);
    insertSession.run("ses_dev_contract", "tenant_default", intents[0].userId, "active", null, createdAt);
    insertSession.run("ses_view_contract", "tenant_default", intents[1].userId, "expired", null, createdAt);
    const insertEvent = database.prepare(`
      INSERT INTO request_log_query_events(query_event_id,actor_user_id,actor_session_id)
      VALUES (?,?,?)
    `);
    insertEvent.run("lqe_contract_developer", intents[0].userId, "ses_dev_contract");
    insertEvent.run("lqe_contract_viewer", intents[1].userId, "ses_view_contract");
  } finally {
    database.close();
  }

  try {
    const config = {
      tenantSlug: "default",
      databasePath: resolveRuntimeDatabasePath(directory),
    };
    const ledger = createTemporaryAccountLedger(
      config,
      Date.parse("2026-07-29T00:00:00.000Z"),
    );
    ledger.intents.push(...intents);
    const first = disableTemporaryUsersAndSessions(config, ledger, { requireAll: true });
    assert.equal(first.matched_user_count, 2);
    assert.equal(first.newly_disabled_user_count, 2);
    assert.equal(first.newly_revoked_session_count, 2);
    assert.equal(first.all_matched_sessions_revoked, true);

    const second = disableTemporaryUsersAndSessions(config, ledger, { requireAll: true });
    assert.equal(second.newly_disabled_user_count, 0);
    assert.equal(second.newly_revoked_session_count, 0);

    database = new DatabaseSync(databasePath, { readOnly: true });
    const temporaryUsers = database.prepare(`
      SELECT role,status FROM users WHERE email LIKE 'prd055-browser-%' ORDER BY role
    `).all();
    assert.deepEqual(
      temporaryUsers.map((row) => ({ role: row.role, status: row.status })),
      [
        { role: "developer", status: "disabled" },
        { role: "viewer", status: "disabled" },
      ],
    );
    assert.equal(
      database.prepare("SELECT status FROM sessions WHERE session_id='ses_admin_contract'").get().status,
      "active",
    );
    assert.equal(
      database.prepare(`
        SELECT COUNT(*) AS count FROM sessions
        WHERE user_id IN (?,?) AND status<>'revoked'
      `).get(intents[0].userId, intents[1].userId).count,
      0,
    );
    assert.equal(database.prepare("SELECT COUNT(*) AS count FROM request_log_query_events").get().count, 2);
    assert.deepEqual(database.prepare("PRAGMA foreign_key_check").all(), []);
  } finally {
    try { database?.close(); } catch { /* 测试清理继续删除本轮临时目录。 */ }
    await rm(directory, { recursive: true, force: true });
  }
});


test("PRD-055 role Session compensation runs only after a successful login", async () => {
  const successCalls = [];
  const success = await runRevocableSession({
    login: async () => { successCalls.push("login"); },
    operation: async () => { successCalls.push("operation"); return "ok"; },
    logout: async () => { successCalls.push("logout"); return "cleared"; },
    revoke: async () => { successCalls.push("revoke"); },
  });
  assert.deepEqual(success, { value: "ok", cleanup: "cleared" });
  assert.deepEqual(successCalls, ["login", "operation", "logout"]);

  const operationCalls = [];
  await assert.rejects(
    () => runRevocableSession({
      login: async () => { operationCalls.push("login"); },
      operation: async () => { operationCalls.push("operation"); throw new Error("expected"); },
      logout: async () => { operationCalls.push("logout"); },
      revoke: async () => { operationCalls.push("revoke"); },
    }),
    /expected/,
  );
  assert.deepEqual(operationCalls, ["login", "operation", "revoke"]);

  const loginCalls = [];
  await assert.rejects(
    () => runRevocableSession({
      login: async () => { loginCalls.push("login"); throw new Error("login"); },
      operation: async () => { loginCalls.push("operation"); },
      logout: async () => { loginCalls.push("logout"); },
      revoke: async () => { loginCalls.push("revoke"); },
    }),
    /login/,
  );
  assert.deepEqual(loginCalls, ["login"]);
});


test("PRD-055 viewer protected-request counters fail closed on any prefetch", () => {
  const baseline = { requestLogs: 4, savedFilters: 3, explanations: 2 };
  assert.equal(assertNoViewerPrefetch(baseline, { ...baseline }), 0);
  assert.throws(
    () => assertNoViewerPrefetch(baseline, { ...baseline, savedFilters: 4 }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "api_prefetched",
  );
});


test("PAGE-080 source keeps SavedFilter selection explicit and exposes stable test anchors", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const renderer = await readFile(
    new URL("../../src/features/request-logs/renderRequestLogPanel.js", import.meta.url),
    "utf8",
  );
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  const acceptance = await readFile(
    new URL("../../../scripts/acceptance/prd055-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );

  const selectionStart = main.indexOf('savedFilterSelect?.addEventListener("change"');
  const selectionEnd = main.indexOf('document.querySelector("#request-log-saved-filter-apply")', selectionStart);
  assert.ok(selectionStart >= 0 && selectionEnd > selectionStart);
  const selection = main.slice(selectionStart, selectionEnd);
  assert.doesNotMatch(selection, /refresh\s*\(/);
  assert.match(selection, /button\.disabled = !savedFilter/);

  const apply = main.slice(selectionEnd, selectionEnd + 1200);
  assert.match(apply, /requestLogFiltersFromSavedFilter\(savedFilter\)/);
  assert.match(renderer, /id="request-log-filter-form"/);
  assert.doesNotMatch(renderer, /data-request-log-explain=/);
  assert.match(renderer, /id="request-log-next"/);

  const prd055 = css.slice(css.indexOf("/* PRD-055 PAGE-080"));
  assert.match(prd055, /\.request-log-saved-filters\s*\{[\s\S]*grid-template-columns:/);
  assert.match(
    prd055,
    /@media \(max-width: 719px\)[\s\S]*\.request-log-query-controls\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/,
  );

  const viewportLoop = acceptance.indexOf("for (const viewport of VIEWPORTS)");
  const savedFilterLifecycle = acceptance.indexOf(
    "runtimeEvidence.runtime.sharedRequestId",
    viewportLoop,
  );
  const loopExit = acceptance.indexOf(
    "await page.setViewportSize({ width: VIEWPORTS[0].width",
    viewportLoop,
  );
  assert.ok(viewportLoop >= 0 && savedFilterLifecycle > viewportLoop && savedFilterLifecycle < loopExit);
  assert.match(acceptance, /screenshots\.length === VIEWPORTS\.length \* 4/);
  assert.match(acceptance, /document\.querySelectorAll\("\.request-logs-panel"\)\]\.find\(visible\)/);
  assert.ok(acceptance.includes('root?.scrollIntoView({ block: "start" })'));
  assert.match(acceptance, /anchor_box:\s*\{\s*x:\s*rootBox\.x/);
  assert.match(acceptance, /rawLayout\?\.anchor_box/);
  assert.ok(acceptance.includes('element.closest("details:not([open])")'));
  assert.match(acceptance, /stateName:\s*"explain-source"/);
  assert.match(acceptance, /sourceRequestId === explainRequestId/);
  assert.match(acceptance, /await explainAction\.click\(\)/);
  assert.doesNotMatch(main, /window\.setInterval\(\(\) => \{[\s\S]*?void refresh\(\);/);
  assert.match(main, /case "request-logs":[\s\S]*loadRequestLogsState\(consoleShellState, refreshId\)/);
  assert.match(main, /bindNavigationHashRefresh\(window, refresh\)/);
  assert.match(acceptance, /"browser\.roles\.viewer",\s*"hash_not_sanitized"/);
});
