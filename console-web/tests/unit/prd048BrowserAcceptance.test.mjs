/**
 * PRD-048 浏览器验收脚本的纯函数与失败生命周期测试。
 *
 * 测试不启动 Chrome、不访问服务，也不写验收制品；固定假凭据只用于证明错误与结果不会泄露值。
 */

import assert from "node:assert/strict";
import test from "node:test";
import {
  BrowserAcceptanceStageError,
  assertExpectedProbeConsoleCount,
  assertNetworkEvidencePrivacy,
  assertRequestLogQualityEvidenceCount,
  assertResultRedacted,
  assertRoleTargetEvidence,
  assertSensitiveSurfaceSnapshot,
  classifyTargetApi,
  collectConfiguredSecrets,
  decideBrowserNetworkRequest,
  executeLogoutWithRetry,
  installBrowserGuards,
  isExpectedProbeConsoleError,
  runAcceptanceStage,
  runRoleLifecycle,
  validateRuntimeStateDocument,
  validateServiceRoot,
} from "../../../scripts/acceptance/prd048-browser-acceptance.mjs";

// 单测固定凭据均为虚构值，覆盖三角色密码、UI Token、Provider Key、Webhook Secret 和正文哨兵。
const TEST_SECRETS = Object.freeze({
  adminPassword: "Prd048!admin-unit-secret",
  developerPassword: "Prd048!developer-unit-secret",
  viewerPassword: "Prd048!viewer-unit-secret",
  uiToken: "unit-ui-session-token-secret",
  providerKey: "sk_unit-provider-key-secret",
  webhookSecret: "whsec_unit-webhook-secret",
  prompt: "prd048-prompt-unit-secret",
  response: "prd048-response-unit-secret",
});

/** 构造满足浏览器消费合同的最小 Runtime 状态，便于逐字段篡改生命周期。 */
function runtimeFixture() {
  return {
    status: "passed",
    prd_id: "PRD-048",
    suffix: "unitrun01",
    lifecycle: {
      retained_for_browser: true,
      cleanup_completed: false,
      cleanup_verified: false,
    },
    resource_ids: {
      main_tenant_id: "tenant_unit",
      admin_user_id: "user_admin_unit",
      developer_user_id: "user_developer_unit",
      viewer_user_id: "user_viewer_unit",
      alias_id: "alias_unit",
      alias_name: "prd048-runtime-unitrun01",
      model_a_id: "model_a_unit",
      model_b_id: "model_b_unit",
    },
    request_ids: { quality_wins: "req_prd048_quality_unit" },
  };
}

/** 返回三角色密码齐备的隔离环境对象，不继承开发机真实凭据。 */
function runtimeEnvironment() {
  return {
    METAHUB_ADMIN_EMAIL: "admin@metahub.local",
    METAHUB_PRD048_ADMIN_PASSWORD: TEST_SECRETS.adminPassword,
    METAHUB_PRD048_DEVELOPER_PASSWORD: TEST_SECRETS.developerPassword,
    METAHUB_PRD048_VIEWER_PASSWORD: TEST_SECRETS.viewerPassword,
  };
}

test("Runtime state accepts only retained and not-cleaned PRD-048 lifecycle", () => {
  const runtime = validateRuntimeStateDocument(runtimeFixture(), { env: runtimeEnvironment() });
  assert.equal(runtime.suffix, "unitrun01");
  assert.equal(runtime.aliasName, "prd048-runtime-unitrun01");
  assert.deepEqual(Object.keys(runtime.accounts).sort(), ["admin", "developer", "viewer"]);

  const mutations = [
    ["wrong PRD", (state) => { state.prd_id = "PRD-047"; }],
    ["not passed", (state) => { state.status = "failed"; }],
    ["not retained", (state) => { state.lifecycle.retained_for_browser = false; }],
    ["already cleaned", (state) => { state.lifecycle.cleanup_completed = true; }],
    ["missing lifecycle", (state) => { delete state.lifecycle; }],
  ];
  for (const [label, mutate] of mutations) {
    const state = structuredClone(runtimeFixture());
    mutate(state);
    assert.throws(
      () => validateRuntimeStateDocument(state, { env: runtimeEnvironment() }),
      Error,
      label,
    );
  }
});

test("configured secret registry covers all role passwords and non-public test credentials", () => {
  const env = {
    ...runtimeEnvironment(),
    METAHUB_PROVIDER_API_KEY: TEST_SECRETS.providerKey,
    METAHUB_WEBHOOK_SECRET: TEST_SECRETS.webhookSecret,
    METAHUB_PROMPT_SENTINEL: TEST_SECRETS.prompt,
    METAHUB_PUBLIC_SUFFIX: "unitrun01",
  };
  const runtime = validateRuntimeStateDocument(runtimeFixture(), { env });
  const secrets = collectConfiguredSecrets(runtime, env);
  for (const secret of [
    TEST_SECRETS.adminPassword,
    TEST_SECRETS.developerPassword,
    TEST_SECRETS.viewerPassword,
    TEST_SECRETS.providerKey,
    TEST_SECRETS.webhookSecret,
    TEST_SECRETS.prompt,
  ]) {
    assert.equal(secrets.has(secret), true);
  }
  assert.equal(secrets.has("unitrun01"), false, "公开批次后缀不应被误判为 Secret");
});

test("service roots and network policy reject arbitrary loopback ports and Gateway business paths", () => {
  assert.equal(validateServiceRoot(new URL("http://localhost:8001"), "8001", "Control"), "http://localhost:8001");
  assert.throws(
    () => validateServiceRoot(new URL("http://localhost:9001"), "8001", "Control"),
    /固定端口/,
  );
  const config = {
    consoleOrigin: "http://localhost:5173",
    controlOrigin: "http://localhost:8001",
    gatewayOrigin: "http://localhost:8000",
  };
  const allowed = [
    { url: "http://localhost:5173/assets/app.js", method: "GET" },
    { url: "http://localhost:8001/model-aliases", method: "GET" },
    {
      url: "http://localhost:8000/health/ready",
      method: "GET",
      headers: { Origin: "http://localhost:5173" },
    },
    {
      url: "http://localhost:8000/version",
      method: "OPTIONS",
      headers: {
        Origin: "http://localhost:5173",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Request-ID",
      },
    },
  ];
  for (const request of allowed) {
    assert.equal(decideBrowserNetworkRequest(request, config).allowed, true);
  }
  const blocked = [
    { url: "http://localhost:61235/collect", method: "GET" },
    { url: "http://127.0.0.1:8001/model-aliases", method: "GET" },
    {
      url: "http://localhost:8000/v1/models",
      method: "GET",
      headers: { Origin: "http://localhost:5173" },
    },
    {
      url: "http://localhost:8000/health?verbose=1",
      method: "GET",
      headers: { Origin: "http://localhost:5173" },
    },
    {
      url: "http://localhost:8000/health",
      method: "GET",
      headers: { Origin: "http://localhost:5173", Authorization: "Bearer fixed-unit-value" },
    },
    { url: "https://localhost:5173/", method: "GET" },
  ];
  for (const request of blocked) {
    assert.equal(decideBrowserNetworkRequest(request, config).allowed, false);
  }
});

test("target API classifier covers ModelAlias, routing policy, score and request log", () => {
  const control = "http://localhost:8001";
  assert.equal(classifyTargetApi(`${control}/model-aliases?status=active`, control), "model_aliases");
  assert.equal(
    classifyTargetApi(`${control}/model-aliases/alias_unit/routing-mode`, control),
    "routing_policy",
  );
  assert.equal(classifyTargetApi(`${control}/model-quality-scores?limit=100`, control), "quality_score");
  assert.equal(classifyTargetApi(`${control}/request-logs`, control), "request_log");
  assert.equal(classifyTargetApi(`${control}/request-logs/req_unit`, control), "request_log");
  assert.equal(classifyTargetApi("http://localhost:8000/model-quality-scores", control), null);
});

test("installed network guard aborts disallowed loopback and keeps only redacted target metadata", async () => {
  const contextListeners = new Map();
  const pageListeners = new Map();
  let routeHandler;
  const context = {
    on(name, handler) { contextListeners.set(name, handler); },
    async route(pattern, handler) {
      assert.equal(pattern, "**/*");
      routeHandler = handler;
    },
  };
  const page = { on(name, handler) { pageListeners.set(name, handler); } };
  const evidence = await installBrowserGuards(context, page, {
    consoleOrigin: "http://localhost:5173",
    controlOrigin: "http://localhost:8001",
    gatewayOrigin: "http://localhost:8000",
  });
  const decisions = [];
  /** 模拟 Playwright request 与 route 的先后事件，验证监听证据和硬阻断使用同一请求。 */
  async function dispatch(url, method = "GET", headers = {}) {
    const lowered = Object.fromEntries(
      Object.entries(headers).map(([name, value]) => [name.toLowerCase(), value]),
    );
    const request = {
      url: () => url,
      method: () => method,
      headers: () => lowered,
      allHeaders: async () => lowered,
    };
    contextListeners.get("request")(request);
    await routeHandler({
      request: () => request,
      continue: async () => decisions.push("continue"),
      abort: async (reason) => decisions.push(`abort:${reason}`),
    });
  }
  await dispatch(
    "http://localhost:8001/model-aliases/alias_unit/routing-mode",
    "PATCH",
    { "X-Request-ID": "req_prd048_browser_rbac_probe_admin_routingPolicy_unitrun01" },
  );
  await dispatch("http://localhost:61235/collect", "GET");
  assert.deepEqual(decisions, ["continue", "abort:blockedbyclient"]);
  assert.equal(evidence.blockedRequestCount, 1);
  assert.deepEqual(evidence.violations, ["origin"]);
  assert.deepEqual(evidence.targetRequests, [{
    sequence: 1,
    method: "PATCH",
    surface: "routing_policy",
    probe: true,
  }]);
  assert.equal(JSON.stringify(evidence).includes("61235"), false, "证据不得保存被拒绝 URL");
  assert.equal(JSON.stringify(evidence).includes("alias_unit"), false, "证据不得保存资源路径 ID");
  assert.equal(pageListeners.has("response"), true);
  evidence.expectedProbeConsoleStatus = 422;
  const consoleListener = pageListeners.get("console");
  for (let index = 0; index < 2; index += 1) {
    consoleListener({
      type: () => "error",
      text: () => "Failed to load resource: the server responded with a status of 422 (Unprocessable Entity)",
    });
  }
  evidence.expectedProbeConsoleStatus = null;
  consoleListener({ type: () => "error", text: () => "unexpected application failure" });
  assert.equal(evidence.expectedProbeConsoleErrorCount, 2);
  assert.deepEqual(evidence.browserErrors, ["console_error"]);
});

test("expected probe console classifier suppresses only the narrow role status window", () => {
  const chrome422 = "Failed to load resource: the server responded with a status of 422 (Unprocessable Entity)";
  const chrome403 = "Failed to load resource: the server responded with a status of 403 (Forbidden)";
  assert.equal(isExpectedProbeConsoleError("error", chrome422, 422), true);
  assert.equal(isExpectedProbeConsoleError("error", chrome403, 403), true);
  assert.equal(isExpectedProbeConsoleError("error", chrome422, 403), false);
  assert.equal(isExpectedProbeConsoleError("error", chrome422, null), false);
  assert.equal(
    isExpectedProbeConsoleError(
      "error",
      "Failed to load resource: the server responded with a status of 500 (Internal Server Error)",
      422,
    ),
    false,
  );
  assert.equal(isExpectedProbeConsoleError("error", "application console failure", 422), false);
  assert.equal(isExpectedProbeConsoleError("warning", chrome422, 422), false);
});

test("expected probe console count allows browser variance but never exceeds confirmed responses", () => {
  for (const observed of [0, 1, 2]) {
    assert.equal(assertExpectedProbeConsoleCount(observed, 2, "admin.write_boundary.verify"), observed);
  }
  let failure;
  try {
    assertExpectedProbeConsoleCount(3, 2, "admin.write_boundary.verify");
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof BrowserAcceptanceStageError);
  assert.equal(failure.stage, "admin.write_boundary.verify");
  assert.equal(failure.code, "expected_probe_console_count_invalid");
});

test("role target evidence proves viewer zero prefetch and exact admin/developer write boundary", () => {
  /** 构造一个读角色的安全目标请求/响应摘要，不包含 URL、查询或 Header 值。 */
  function evidenceFor(role) {
    const writeStatus = role === "admin" ? 422 : 403;
    return {
      targetRequests: [
        { method: "GET", surface: "model_aliases", probe: false },
        { method: "GET", surface: "quality_score", probe: false },
        { method: "GET", surface: "request_log", probe: false },
        { method: "GET", surface: "request_log", probe: false },
        { method: "PATCH", surface: "routing_policy", probe: true },
        { method: "PATCH", surface: "quality_score", probe: true },
      ],
      targetResponses: [
        ...["model_aliases", "quality_score", "request_log", "request_log"].map((surface) => ({
          method: "GET",
          surface,
          status: 200,
          // ModelAlias 是既有接口；PRD-048 的 no-store 合同只覆盖质量评分和调用日志。
          noStore: surface !== "model_aliases",
          requestIdPresent: true,
          traceIdPresent: true,
        })),
        { method: "PATCH", surface: "routing_policy", status: writeStatus },
        { method: "PATCH", surface: "quality_score", status: writeStatus },
      ],
    };
  }
  assert.equal(assertRoleTargetEvidence("admin", evidenceFor("admin")).writeProbeExpectedStatus, 422);
  assert.equal(assertRoleTargetEvidence("developer", evidenceFor("developer")).writeProbeExpectedStatus, 403);
  assert.deepEqual(assertRoleTargetEvidence("viewer", { targetRequests: [], targetResponses: [] }), {
    targetRequestCount: 0,
    targetPrefetchCount: 0,
    writeProbeCount: 0,
  });
  assert.throws(
    () => assertRoleTargetEvidence("viewer", {
      targetRequests: [{ method: "GET", surface: "model_aliases" }],
      targetResponses: [],
    }),
    /零预取/,
  );
  const unsafeDeveloper = evidenceFor("developer");
  unsafeDeveloper.targetRequests.push({ method: "POST", surface: "model_aliases", probe: false });
  assert.throws(() => assertRoleTargetEvidence("developer", unsafeDeveloper), /数量异常/);

  const missingAliasTrace = evidenceFor("admin");
  missingAliasTrace.targetResponses.find((item) => item.surface === "model_aliases").traceIdPresent = false;
  assert.throws(() => assertRoleTargetEvidence("admin", missingAliasTrace), /request_id\/trace_id/);

  for (const surface of ["quality_score", "request_log"]) {
    const cacheableSensitiveRead = evidenceFor("admin");
    cacheableSensitiveRead.targetResponses.find((item) => item.surface === surface).noStore = false;
    assert.throws(() => assertRoleTargetEvidence("admin", cacheableSensitiveRead), /no-store/);
  }
});

test("privacy scanner covers three passwords, UI token, test keys and every storage surface", () => {
  const exactSecrets = Object.values(TEST_SECRETS);
  const safe = {
    domText: "质量成本路由",
    domHtml: "<main>质量成本路由</main>",
    url: "http://localhost:5173/#routing-policies",
    localStorageEntries: [["theme", "dark"]],
    sessionStorageEntries: [
      ["metahub.auth_token", TEST_SECRETS.uiToken],
      ["metahub.auth_context", '{"role":"admin"}'],
    ],
  };
  assert.equal(assertSensitiveSurfaceSnapshot(safe, {
    exactSecrets,
    activeToken: TEST_SECRETS.uiToken,
  }).authStorageContractValid, true);

  const unsafeSnapshots = [
    { ...safe, domText: TEST_SECRETS.adminPassword },
    { ...safe, domHtml: `<main>${TEST_SECRETS.developerPassword}</main>` },
    { ...safe, url: `http://localhost:5173/?key=${TEST_SECRETS.providerKey}` },
    { ...safe, localStorageEntries: [["debug", TEST_SECRETS.viewerPassword]] },
    {
      ...safe,
      sessionStorageEntries: [
        ["metahub.auth_token", TEST_SECRETS.uiToken],
        ["metahub.auth_context", TEST_SECRETS.webhookSecret],
      ],
    },
  ];
  for (const snapshot of unsafeSnapshots) {
    let failure;
    try {
      assertSensitiveSurfaceSnapshot(snapshot, {
        exactSecrets,
        activeToken: TEST_SECRETS.uiToken,
      });
    } catch (error) {
      failure = error;
    }
    assert.ok(failure instanceof Error);
    for (const secret of exactSecrets) assert.equal(failure.message.includes(secret), false);
  }
  assert.throws(
    () => assertSensitiveSurfaceSnapshot(safe, {
      exactSecrets,
      activeToken: null,
      loggedOut: true,
    }),
    /注销后仍保留认证槽位/,
  );
});

test("network metadata and final result scans reject credentials without echoing them", () => {
  const exactSecrets = Object.values(TEST_SECRETS);
  const safeEvidence = {
    blockedRequestCount: 0,
    violations: [],
    browserErrors: [],
    networkMetadata: [{ sequence: 1, method: "GET", destination: "control", targetSurface: "quality_score" }],
    targetRequests: [{ sequence: 1, method: "GET", surface: "quality_score", probe: false }],
    targetResponses: [{ sequence: 1, method: "GET", surface: "quality_score", status: 200 }],
  };
  assert.equal(assertNetworkEvidencePrivacy(safeEvidence, exactSecrets).networkSensitiveHitCount, 0);
  assert.equal(assertResultRedacted({ status: "passed", requestCount: 1 }, exactSecrets), true);
  for (const unsafe of [
    { ...safeEvidence, networkMetadata: [{ destination: TEST_SECRETS.providerKey }] },
    { ...safeEvidence, targetRequests: [{ surface: TEST_SECRETS.prompt }] },
    { ...safeEvidence, targetResponses: [{ code: TEST_SECRETS.response }] },
  ]) {
    let failure;
    try {
      assertNetworkEvidencePrivacy(unsafe, exactSecrets);
    } catch (error) {
      failure = error;
    }
    assert.ok(failure instanceof Error);
    for (const secret of exactSecrets) assert.equal(failure.message.includes(secret), false);
  }
  assert.throws(
    () => assertResultRedacted({ status: "passed", accidental: TEST_SECRETS.uiToken }, exactSecrets),
    /敏感信息/,
  );
});

test("request log quality evidence uses the ARIA group count instead of innerText", () => {
  assert.equal(assertRequestLogQualityEvidenceCount(2, "admin.request_log.verify"), true);
  let failure;
  try {
    assertRequestLogQualityEvidenceCount(0, "admin.request_log.verify");
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof BrowserAcceptanceStageError);
  assert.equal(failure.stage, "admin.request_log.verify");
  assert.equal(failure.code, "request_log_quality_evidence_missing");
});

test("stage wrapper preserves fixed diagnostics without leaking the underlying failure", async () => {
  let failure;
  try {
    await runAcceptanceStage("admin.request_log.verify", async () => {
      // 原始异常故意混入 Token、URL 查询和密码，验证阶段收敛不会复制任何底层 message。
      throw new Error(`${TEST_SECRETS.uiToken}?query=${TEST_SECRETS.adminPassword}`);
    });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof BrowserAcceptanceStageError);
  assert.equal(failure.stage, "admin.request_log.verify");
  assert.equal(failure.code, "stage_failed");
  assert.equal(failure.message.includes(TEST_SECRETS.uiToken), false);
  assert.equal(failure.message.includes(TEST_SECRETS.adminPassword), false);
  assert.equal(Object.hasOwn(failure, "cause"), false);
});

test("logout retries a missing response and returns auditable redacted success", async () => {
  let attempts = 0;
  let verificationCount = 0;
  const audit = await executeLogoutWithRetry({
    attemptAction: async () => {
      attempts += 1;
      // 第一次用含 Secret 的原始异常模拟 Playwright 失败，生产 helper 必须只登记固定类别。
      if (attempts === 1) throw new Error(TEST_SECRETS.uiToken);
      // 第二次携带虚构响应正文，审计结果仍只能保留 HTTP 状态。
      return { status: 200, responseBody: TEST_SECRETS.response };
    },
    verifyAction: async () => { verificationCount += 1; },
  });
  assert.deepEqual(audit, {
    completed: true,
    attemptCount: 2,
    responseStatuses: ["attempt_error", 200],
    authStorageCleared: true,
  });
  assert.equal(verificationCount, 1);
  assert.equal(JSON.stringify(audit).includes(TEST_SECRETS.uiToken), false);
  assert.equal(JSON.stringify(audit).includes(TEST_SECRETS.response), false);
});

test("logout terminal failure is strict, bounded and does not leak underlying credentials", async () => {
  let attempts = 0;
  let failure;
  try {
    await executeLogoutWithRetry({
      attemptAction: async () => {
        attempts += 1;
        // 两次底层异常都故意包含密码，用于证明最终错误与 audit 不会回显该值。
        throw new Error(TEST_SECRETS.adminPassword);
      },
      verifyAction: async () => undefined,
    });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof Error);
  assert.equal(attempts, 2);
  assert.equal(failure.message.includes(TEST_SECRETS.adminPassword), false);
  assert.deepEqual(failure.audit, {
    completed: false,
    attemptCount: 2,
    responseStatuses: ["attempt_error", "attempt_error"],
    authStorageCleared: false,
  });
});

test("role lifecycle never swallows logout failure and still closes the browser context", async () => {
  let closeCount = 0;
  let failure;
  try {
    await runRoleLifecycle({
      stagePrefix: "admin",
      executeAction: async () => ({ status: "page_passed" }),
      logoutAction: async () => { throw new Error(TEST_SECRETS.uiToken); },
      closeAction: async () => { closeCount += 1; },
    });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof AggregateError);
  assert.equal(closeCount, 1);
  assert.match(failure.message, /生命周期失败/);
  assert.equal(failure.message.includes(TEST_SECRETS.uiToken), false);
  const logoutFailure = failure.errors.find((error) => error.stage === "admin.logout");
  assert.ok(logoutFailure instanceof BrowserAcceptanceStageError);
  assert.equal(logoutFailure.code, "logout_failed");
});
