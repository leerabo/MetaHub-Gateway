/**
 * PRD-049 真实浏览器验收脚本的轻量门禁测试。
 *
 * 本文件不启动 Chrome、不访问三个本地服务，也不写验收制品；固定虚构凭据只用于证明
 * Runtime/路径/网络/隐私/角色和 finally 清理合同会在异常路径严格失败且不回显原值。
 */

import assert from "node:assert/strict";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";

import {
  BrowserAcceptanceStageError,
  assertBackendPermissionEvidence,
  assertPrd049DatabaseHardening,
  assertResultRedacted,
  assertRoleTargetEvidence,
  assertSensitiveSurfaceSnapshot,
  classifyTargetApi,
  collectConfiguredSecrets,
  decideBrowserNetworkRequest,
  deriveFeedbackExpectations,
  executeLogoutWithRetry,
  resolveBatchPaths,
  resolveRuntimeDatabasePath,
  runAcceptanceStage,
  runOwnedStateLifecycle,
  runRoleLifecycle,
  summaryDomMatchesStableFacts,
  toSafeStageError,
  validateRuntimeStateDocument,
} from "../../../scripts/acceptance/prd049-browser-acceptance.mjs";

// 所有 Secret 都是单测虚构值；断言只检查布尔结论，失败输出不得包含这些字符串。
const TEST_SECRETS = Object.freeze({
  adminPassword: "Prd049Browser!admin-unit-secret",
  developerPassword: "Prd049Browser!developer-unit-secret",
  viewerPassword: "Prd049Browser!viewer-unit-secret",
  uiToken: "unit-prd049-ui-session-token-secret",
  providerKey: "sk-unit-prd049-provider-key-secret",
  virtualKey: "mhv_unit-prd049-virtual-key-secret",
  prompt: "prd048-prompt-unit-prd049-secret",
  response: "selected-a",
});

// 测试端独立冻结名称，防止生产查询和生产 Set 同时漏掉某项时测试仍误通过。
const EXPECTED_HARDENING_TRIGGERS = Object.freeze([
  "trg_request_feedback_conflict_insert_prd049",
  "trg_request_feedback_event_conflict_insert_prd049",
  "trg_request_log_feedback_parent_conflict_insert_prd049",
  "trg_request_log_feedback_parent_conflict_update_prd049",
  "trg_routing_attempt_feedback_parent_conflict_insert_prd049",
  "trg_routing_attempt_feedback_parent_conflict_update_prd049",
]);

/** 构造浏览器可消费的最小脱敏 Runtime 状态。 */
function runtimeFixture() {
  return {
    status: "passed",
    prd_id: "PRD-049",
    suffix: "unitrun01",
    database_file: "metahub-prd049.db",
    lifecycle: {
      credentials_cleanup_completed: true,
      credentials_cleanup_verified: true,
      credentials_changed: 8,
    },
    resource_ids: {
      main_tenant_id: "tenant_unit",
      admin_user_id: "user_admin_unit",
      model_a_id: "model_a_unit",
    },
    source_request_ids: [
      "req_prd049_runtime_session_source_unitrun01",
      "req_prd049_runtime_key_source_unitrun01",
    ],
    checks: {
      admin_summary_read: true,
      developer_summary_read: true,
      viewer_feedback_denied_before_body: true,
      viewer_summary_denied: true,
      migration_0039_applied: true,
      migration_0039_trigger_count: 6,
      control_feedback_method_not_allowed: true,
      gateway_feedback_method_not_allowed: true,
      feedback_method_not_allowed_parity: true,
      request_log_feedback_join: true,
      database_aggregate_exact: true,
      database_sensitive_hits: 0,
      log_sensitive_hits: 0,
    },
  };
}

/** 构造 Runtime 同模型的两条不可变 Feedback 数据库行。 */
function feedbackRows() {
  return [
    {
      source_request_id: "req_prd049_session_unit",
      request_feedback_id: "rfb_11111111111111111111111111111111",
      model_id: "model_a_unit",
      rating: 5,
      accepted: 1,
      tags_json: '["accurate","helpful"]',
      actor_type: "session_user",
      created_at: "2026-07-20T08:00:00Z",
    },
    {
      source_request_id: "req_prd049_key_unit",
      request_feedback_id: "rfb_22222222222222222222222222222222",
      model_id: "model_a_unit",
      rating: 4,
      accepted: 0,
      tags_json: '["relevant","clear"]',
      actor_type: "virtual_key",
      created_at: "2026-07-20T08:00:01Z",
    },
  ];
}

test("runtime state accepts only passed PRD-049 with cleaned credentials and no secret fields", () => {
  const runtime = validateRuntimeStateDocument(runtimeFixture());
  assert.equal(runtime.suffix, "unitrun01");
  assert.equal(runtime.databaseFile, "metahub-prd049.db");
  assert.deepEqual(runtime.sourceRequestIds, [
    "req_prd049_runtime_session_source_unitrun01",
    "req_prd049_runtime_key_source_unitrun01",
  ]);

  for (const unsafe of [
    { ...runtimeFixture(), status: "failed" },
    { ...runtimeFixture(), prd_id: "PRD-048" },
    { ...runtimeFixture(), lifecycle: { credentials_cleanup_completed: false, credentials_cleanup_verified: false } },
    { ...runtimeFixture(), access_token: TEST_SECRETS.uiToken },
    { ...runtimeFixture(), source_request_ids: ["same", "same"] },
  ]) {
    let failure;
    try {
      validateRuntimeStateDocument(unsafe);
    } catch (error) {
      failure = error;
    }
    assert.ok(failure instanceof Error);
    for (const secret of Object.values(TEST_SECRETS)) {
      assert.equal(failure.message.includes(secret), false);
    }
  }
});

test("database gate requires migration 0039 and both conflict update triggers", () => {
  const database = new DatabaseSync(":memory:");
  try {
    database.exec(
      "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);"
        + "INSERT INTO schema_migrations(version) VALUES (39);"
        + "CREATE TABLE request_feedbacks(probe_id INTEGER PRIMARY KEY);",
    );
    for (const triggerName of EXPECTED_HARDENING_TRIGGERS) {
      database.exec(
        'CREATE TRIGGER "' + triggerName + '" BEFORE INSERT ON request_feedbacks '
          + "BEGIN SELECT 1; END",
      );
    }
    assert.deepEqual(assertPrd049DatabaseHardening(database), {
      migrationVersion: 39,
      triggerCount: 6,
    });

    for (const triggerName of [
      "trg_request_log_feedback_parent_conflict_update_prd049",
      "trg_routing_attempt_feedback_parent_conflict_update_prd049",
    ]) {
      database.exec('DROP TRIGGER "' + triggerName + '"');
      assert.throws(() => assertPrd049DatabaseHardening(database));
      database.exec(
        'CREATE TRIGGER "' + triggerName + '" BEFORE INSERT ON request_feedbacks '
          + "BEGIN SELECT 1; END",
      );
    }

    database.exec("UPDATE schema_migrations SET version=38 WHERE version=39");
    assert.throws(() => assertPrd049DatabaseHardening(database));
  } finally {
    database.close();
  }
});

test("batch and database paths stay inside one artifacts/prd049 batch", () => {
  const workspace = path.resolve("D:\\MetaHub");
  const paths = resolveBatchPaths(workspace, "20260720T120000");
  assert.equal(paths.batchDirectory, path.join(workspace, "artifacts", "prd049", "20260720T120000"));
  assert.equal(
    resolveRuntimeDatabasePath(paths.batchDirectory, "metahub-prd049.db"),
    path.join(paths.batchDirectory, "metahub-prd049.db"),
  );
  for (const batch of ["", ".", "..", "../other", "nested/batch", "C:\\outside"]) {
    assert.throws(() => resolveBatchPaths(workspace, batch));
  }
  for (const filename of ["../other.db", "nested/other.db", "other.sqlite", "C:\\other.db"]) {
    assert.throws(() => resolveRuntimeDatabasePath(paths.batchDirectory, filename));
  }
});

test("feedback expectations derive exact PAGE-070 and PAGE-071 integer evidence", () => {
  const expected = deriveFeedbackExpectations(feedbackRows());
  assert.equal(expected.detailFeedback.actorType, "session_user");
  assert.equal(expected.modelId, "model_a_unit");
  assert.equal(expected.fromTime, "2026-07-20T07:59:59Z");
  assert.equal(expected.toTime, "2026-07-20T08:00:02Z");
  assert.equal(expected.feedbackCount, 2);
  assert.equal(expected.ratingSum, 9);
  assert.equal(expected.averageRatingMilli, 4500);
  assert.equal(expected.acceptedCount, 1);
  assert.equal(expected.adoptionRateBps, 5000);
  assert.deepEqual(expected.ratingDistribution, { 1: 0, 2: 0, 3: 0, 4: 1, 5: 1 });
  assert.deepEqual(expected.tagCounts, [
    { tag: "accurate", count: 1 },
    { tag: "relevant", count: 1 },
    { tag: "helpful", count: 1 },
    { tag: "clear", count: 1 },
  ]);
  assert.throws(() => deriveFeedbackExpectations(feedbackRows().slice(0, 1)));
  assert.throws(() => deriveFeedbackExpectations([
    feedbackRows()[0],
    { ...feedbackRows()[1], model_id: "model_other" },
  ]));
});

test("filtered summary DOM gate rejects stale or incomplete snapshots", () => {
  const stableFacts = {
    ...deriveFeedbackExpectations(feedbackRows()),
    modelAlias: "model-alias-unit",
    modelDisplayName: "Unit Model",
  };
  // 初始全租户快照包含 4 条反馈和 2 个模型，即使请求已返回也不能作为筛选完成证据。
  const initialSnapshot = {
    metricStrongValues: ["4", "3.75 / 5", "50%", "2"],
    modelRows: [
      { text: "Unit Model model-alias-unit 2", feedbackCount: "2" },
      { text: "Other Model model-alias-other 2", feedbackCount: "2" },
    ],
    tagRows: [
      { label: "准确", count: "1" },
      { label: "相关", count: "1" },
      { label: "有帮助", count: "1" },
      { label: "清晰", count: "1" },
    ],
  };
  assert.equal(summaryDomMatchesStableFacts(initialSnapshot, stableFacts), false);

  // 筛选快照必须同时满足四个 strong、唯一模型行和全部预期标签，才允许继续截图。
  const filteredSnapshot = {
    metricStrongValues: ["2", "4.5 / 5", "50%", "1"],
    modelRows: [
      {
        text: "Unit Model model-alias-unit 2 4.5 / 5 1 50%",
        feedbackCount: "2",
      },
    ],
    tagRows: [
      { label: "准确", count: "1" },
      { label: "相关", count: "1" },
      { label: "有帮助", count: "1" },
      { label: "清晰", count: "1" },
    ],
  };
  assert.equal(summaryDomMatchesStableFacts(filteredSnapshot, stableFacts), true);
  assert.equal(summaryDomMatchesStableFacts({
    ...filteredSnapshot,
    tagRows: filteredSnapshot.tagRows.slice(0, -1),
  }, stableFacts), false);
});

test("network decision allows fixed loopback services and blocks exfiltration or Gateway credentials", () => {
  assert.deepEqual(
    decideBrowserNetworkRequest({ url: "http://localhost:5173/src/app/main.js" }),
    { allowed: true, destination: "console", violation: null },
  );
  assert.deepEqual(
    decideBrowserNetworkRequest({ url: "http://localhost:8001/feedback/summary?limit=20" }),
    { allowed: true, destination: "control", violation: null },
  );
  assert.deepEqual(
    decideBrowserNetworkRequest({
      url: "http://localhost:8000/health",
      headers: { Origin: "http://localhost:5173" },
    }),
    { allowed: true, destination: "gateway_health", violation: null },
  );
  assert.equal(decideBrowserNetworkRequest({
    url: "http://localhost:8000/health",
    headers: { Origin: "http://localhost:5173", Authorization: `Bearer ${TEST_SECRETS.uiToken}` },
  }).allowed, false);
  assert.equal(decideBrowserNetworkRequest({ url: "https://example.invalid/collect" }).allowed, false);
  assert.equal(decideBrowserNetworkRequest({ url: "http://localhost:9000/feedback" }).allowed, false);
});

test("target API classifier never retains query values and separates list/detail/summary/post", () => {
  assert.equal(classifyTargetApi("http://localhost:8001/request-logs?request_id=secret"), "request_log_list");
  assert.equal(classifyTargetApi("http://localhost:8001/request-logs/req_unit"), "request_log_detail");
  assert.equal(classifyTargetApi("http://localhost:8001/feedback/summary?model_id=model_unit"), "feedback_summary");
  assert.equal(classifyTargetApi("http://localhost:8001/feedback"), "feedback_post");
  assert.equal(classifyTargetApi("http://localhost:8000/feedback"), null);
  assert.equal(classifyTargetApi("not-a-url"), null);
});

test("DOM URL and storage privacy allows only the active token slot", () => {
  const exactSecrets = collectConfiguredSecrets(Object.values(TEST_SECRETS), {});
  const safe = {
    domText: "已记录反馈 5 / 5 已采纳",
    domHtml: '<section class="request-feedback-recorded">已记录反馈</section>',
    url: "http://localhost:5173/#request-logs",
    localStorageEntries: [],
    sessionStorageEntries: [
      ["metahub.auth_token", TEST_SECRETS.uiToken],
      ["metahub.auth_context", '{"role":"developer"}'],
    ],
  };
  assert.equal(assertSensitiveSurfaceSnapshot(safe, {
    exactSecrets,
    activeToken: TEST_SECRETS.uiToken,
  }).authStorageContractValid, true);
  for (const unsafe of [
    { ...safe, domText: TEST_SECRETS.prompt },
    { ...safe, domHtml: `<main>${TEST_SECRETS.providerKey}</main>` },
    { ...safe, url: `http://localhost:5173/?token=${TEST_SECRETS.uiToken}` },
    { ...safe, localStorageEntries: [["debug", TEST_SECRETS.viewerPassword]] },
    {
      ...safe,
      sessionStorageEntries: [
        ["metahub.auth_token", TEST_SECRETS.uiToken],
        ["metahub.auth_context", TEST_SECRETS.virtualKey],
      ],
    },
  ]) {
    let failure;
    try {
      assertSensitiveSurfaceSnapshot(unsafe, { exactSecrets, activeToken: TEST_SECRETS.uiToken });
    } catch (error) {
      failure = error;
    }
    assert.ok(failure instanceof Error);
    for (const secret of exactSecrets) assert.equal(failure.message.includes(secret), false);
  }
  assert.equal(assertSensitiveSurfaceSnapshot({
    ...safe,
    sessionStorageEntries: [],
  }, { exactSecrets, activeToken: null, loggedOut: true }).authStorageContractValid, true);
});

test("role evidence requires both reader pages while viewer stays zero DOM prefetch", () => {
  const reader = {
    targetRequests: [
      { method: "GET", surface: "request_log_list" },
      { method: "GET", surface: "request_log_detail" },
      { method: "GET", surface: "feedback_summary" },
    ],
    targetResponses: [
      { method: "GET", surface: "request_log_list", status: 200, requestIdPresent: true, traceIdPresent: true, noStore: true },
      { method: "GET", surface: "request_log_detail", status: 200, requestIdPresent: true, traceIdPresent: true, noStore: true },
      { method: "GET", surface: "feedback_summary", status: 200, requestIdPresent: true, traceIdPresent: true, noStore: true },
    ],
  };
  assert.equal(assertRoleTargetEvidence("developer", reader).feedbackPostCount, 0);
  assert.deepEqual(assertRoleTargetEvidence("viewer", { targetRequests: [], targetResponses: [] }), {
    targetRequestCount: 0,
    targetResponseCount: 0,
    targetPrefetchCount: 0,
  });
  assert.throws(() => assertRoleTargetEvidence("viewer", reader));
  assert.throws(() => assertRoleTargetEvidence("admin", {
    ...reader,
    targetRequests: [...reader.targetRequests, { method: "POST", surface: "feedback_post" }],
  }));
});

test("backend permission matrix distinguishes allowed parsing from viewer pre-body denial", () => {
  const evidence = {
    admin: { summaryStatus: 200, feedbackPostStatus: 422, requestLogStatus: 200 },
    developer: { summaryStatus: 200, feedbackPostStatus: 422, requestLogStatus: 200 },
    viewer: { summaryStatus: 403, feedbackPostStatus: 403, requestLogStatus: 403 },
  };
  assert.equal(assertBackendPermissionEvidence(evidence).backendRbacVerified, true);
  assert.throws(() => assertBackendPermissionEvidence({
    ...evidence,
    viewer: { ...evidence.viewer, feedbackPostStatus: 422 },
  }));
});

test("stage wrapper does not copy a credential-bearing underlying error", async () => {
  let failure;
  try {
    await runAcceptanceStage("developer.page070.verify", async () => {
      // 底层异常故意混入 Token 和密码，验证生产 wrapper 只传播固定阶段与错误码。
      throw new Error(`${TEST_SECRETS.uiToken}?password=${TEST_SECRETS.adminPassword}`);
    });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof BrowserAcceptanceStageError);
  assert.equal(failure.stage, "developer.page070.verify");
  assert.equal(failure.code, "stage_failed");
  assert.equal(failure.message.includes(TEST_SECRETS.uiToken), false);
  assert.equal(failure.message.includes(TEST_SECRETS.adminPassword), false);
});

test("safe stage recovery unwraps one nested failure but keeps multi-failure generic", () => {
  const precise = new BrowserAcceptanceStageError("admin.page071.verify", "assertion_failed");
  const nested = new AggregateError([new AggregateError([precise], "inner")], "outer");
  assert.equal(toSafeStageError(nested, "browser.main", "acceptance_failed"), precise);

  const multiple = new AggregateError([
    precise,
    new Error(`${TEST_SECRETS.uiToken}?password=${TEST_SECRETS.adminPassword}`),
  ], "multiple");
  const generic = toSafeStageError(multiple, "browser.main", "acceptance_failed");
  assert.ok(generic instanceof BrowserAcceptanceStageError);
  assert.equal(generic.stage, "browser.main");
  assert.equal(generic.code, "acceptance_failed");
  assert.equal(generic.message.includes(TEST_SECRETS.uiToken), false);
  assert.equal(generic.message.includes(TEST_SECRETS.adminPassword), false);
});

test("owned lifecycle always runs cleanup and never returns partial success", async () => {
  let cleanupCount = 0;
  let failure;
  try {
    await runOwnedStateLifecycle({
      executeAction: async () => { throw new Error(TEST_SECRETS.providerKey); },
      cleanupAction: async () => {
        cleanupCount += 1;
        return { verified: true };
      },
    });
  } catch (error) {
    failure = error;
  }
  assert.equal(cleanupCount, 1);
  assert.ok(failure instanceof AggregateError);
  assert.equal(failure.message.includes(TEST_SECRETS.providerKey), false);

  const passed = await runOwnedStateLifecycle({
    executeAction: async () => ({ page: "passed" }),
    cleanupAction: async () => ({ verified: true }),
  });
  assert.deepEqual(passed, { result: { page: "passed" }, cleanup: { verified: true } });
});

test("role lifecycle records logout failure and still closes its context", async () => {
  let closeCount = 0;
  let failure;
  try {
    await runRoleLifecycle({
      stagePrefix: "viewer",
      executeAction: async () => ({ zeroDom: true }),
      logoutAction: async () => { throw new Error(TEST_SECRETS.uiToken); },
      closeAction: async () => { closeCount += 1; },
    });
  } catch (error) {
    failure = error;
  }
  assert.equal(closeCount, 1);
  assert.ok(failure instanceof AggregateError);
  const logoutFailure = failure.errors.find((item) => item.stage === "viewer.logout");
  assert.ok(logoutFailure instanceof BrowserAcceptanceStageError);
  assert.equal(logoutFailure.code, "logout_failed");
  assert.equal(failure.message.includes(TEST_SECRETS.uiToken), false);
});

test("logout retry audit contains only bounded statuses and no response body", async () => {
  let attempts = 0;
  let verifications = 0;
  const audit = await executeLogoutWithRetry({
    attemptAction: async () => {
      attempts += 1;
      // 第一次用含 Token 的异常模拟响应丢失，生产 helper 不得把原文写入 audit。
      if (attempts === 1) throw new Error(TEST_SECRETS.uiToken);
      // 第二次响应故意带正文哨兵，返回审计仍只能选择 HTTP 状态字段。
      return { status: 200, responseBody: TEST_SECRETS.response };
    },
    verifyAction: async () => { verifications += 1; },
  });
  assert.deepEqual(audit, {
    completed: true,
    attemptCount: 2,
    responseStatuses: ["attempt_error", 200],
    authStorageCleared: true,
  });
  assert.equal(verifications, 1);
  assert.equal(JSON.stringify(audit).includes(TEST_SECRETS.response), false);
});

test("final result redaction rejects password token key prompt and response sentinels", () => {
  const exactSecrets = Object.values(TEST_SECRETS);
  assert.equal(assertResultRedacted({ status: "passed", screenshotCount: 15 }, exactSecrets), true);
  for (const secret of exactSecrets) {
    assert.throws(() => assertResultRedacted({ status: "passed", accidental: secret }, exactSecrets));
  }
});
