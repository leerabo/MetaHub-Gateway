/**
 * PRD-050 真实浏览器验收脚本的轻量合同测试。
 *
 * 本文件不启动 Chrome、不访问三服务、不写 SQLite 或验收制品；固定虚构值仅用于证明
 * Runtime/路径/网络/隐私/RBAC/owner/finally 合同会严格失败关闭且不会回显敏感原文。
 */

import assert from "node:assert/strict";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";

import {
  BrowserAcceptanceStageError,
  GUARDED_ROUTE_FETCH_OPTIONS,
  PAGE073_EXPECTATIONS,
  REQUIRED_RUNTIME_REQUEST_KEYS,
  REQUIRED_VIEWPORT_STATES,
  SAFE_SELECTION_FIELDS,
  VIEWPORTS,
  assertBackendPermissionEvidence,
  assertBrowserGuardEvidence,
  assertGuardedRouteFetchRequest,
  backendPermissionExpectation,
  assertCacheEvidenceIndependent,
  assertConflictRefreshEvidence,
  assertLayoutMetrics,
  assertPage072LoadingEvidence,
  assertPage073DetailPayload,
  assertPhysicalPathInside,
  assertRetryCancelEvidence,
  assertResultRedacted,
  assertRoleTargetEvidence,
  assertSensitiveSurfaceSnapshot,
  assertSessionSwitchEvidence,
  assertTransportConsoleConverged,
  assertUrlPrivacy,
  assertViewportStateMatrix,
  beginExpectedTransportConsoleError,
  classifyTargetApi,
  classifyUnexpectedBrowserConsoleError,
  collectConfiguredSecrets,
  configSemantics,
  configSemanticsEqual,
  createBrowserEvidenceDirectory,
  createBrowserRouteFailureSupervisor,
  createDeferred,
  createResultTemporaryPath,
  decideBrowserNetworkRequest,
  executeLogoutWithRetry,
  fallThroughBrowserRoute,
  hasSingleExactResponseHeader,
  installAfterCommitPatchGate,
  installBeforeSendPatchGate,
  recordBrowserConsoleError,
  normalizeLoopbackOrigin,
  observePromiseOutcome,
  parseBrowserArgs,
  pathIsInside,
  projectTargetResponseBody,
  publishWriteOnceResult,
  resolveRuntimeDatabasePath,
  resolveAcceptanceRoots,
  runAcceptanceStage,
  runBestEffortCleanupStages,
  runBrowserActionWithObservedResponse,
  runBrowserCloseLifecycle,
  runOwnedStateLifecycle,
  runRoleLifecycle,
  scheduleTargetResponseScan,
  screenshotRoleStyle,
  settleBrowserRouteBeforeUnroute,
  settleExpectedTransportConsoleError,
  settleMutation,
  superviseBrowserRoute,
  targetCorsOriginValid,
  targetRequestCorsOriginValid,
  targetPreflightHeadersValid,
  toSafeStageError,
  validateRuntimeStateDocument,
  writeNewEvidenceFile,
} from "../../../scripts/acceptance/prd050-browser-acceptance.mjs";

// 三个回环 origin 与测试 Secret 只构造虚假合同输入，不连接服务或写入验收制品。
const ORIGINS = Object.freeze({
  consoleOrigin: "http://127.0.0.1:15173",
  controlOrigin: "http://127.0.0.1:18001",
  gatewayOrigin: "http://127.0.0.1:18000",
});
// 正式 Browser 由 runner 在该固定 cwd 启动；参数解析单测显式注入逻辑根，避免依赖 npm 的 cwd。
const TEST_WORKSPACE_ROOT = path.resolve("D:\\MetaHub");
const TEST_ARTIFACT_ROOT = path.resolve(TEST_WORKSPACE_ROOT, "artifacts", "prd050");
const TEST_GENERATION = "20260723T010203";
const TEST_RUNTIME_STATE_PATH = path.join(TEST_ARTIFACT_ROOT, TEST_GENERATION, "runtime-state.json");
const TEST_ACCEPTANCE_ROOTS = Object.freeze({
  workspaceRoot: TEST_WORKSPACE_ROOT,
  artifactRoot: TEST_ARTIFACT_ROOT,
});
// 每类敏感值使用不同哨兵，确保 DOM、URL、Storage、错误和结果扫描均可独立失败关闭。
const TEST_SECRETS = Object.freeze({
  adminPassword: "Prd050Browser!admin-unit-secret",
  developerPassword: "Prd050Browser!developer-unit-secret",
  viewerPassword: "Prd050Browser!viewer-unit-secret",
  uiToken: "unit-prd050-ui-session-token-secret",
  providerKey: "sk-unit-prd050-provider-key-secret",
  virtualKey: "mhv_unit_prd050_virtual_key_secret",
  prompt: "prd050-long-unit-prompt-secret",
  response: "prd050-response-unit-secret",
});

/** 汇总公开错误的 message/stack/cause 表面，确保任何诊断属性都不附带底层 Secret。 */
function errorDiagnosticSurface(errors) {
  return errors.flatMap((error) => [
    String(error?.message ?? ""),
    String(error?.stack ?? ""),
    String(error?.cause?.message ?? error?.cause ?? ""),
  ]).join("\n");
}

/** 构造 Context close 终检使用的最小浏览器守卫账本，不连接页面或真实网络。 */
function browserGuardEvidenceFixture(overrides = {}) {
  return {
    blockedRequestCount: 0,
    violations: [],
    browserErrors: [],
    expectedTransportErrorCount: 0,
    observedTransportErrorCount: 0,
    settledTransportErrorCount: 0,
    transportErrorWindows: [],
    pendingResponseScans: new Set(),
    ...overrides,
  };
}

/** 构造一条已完成隐私、CORS 与统一链路头检查的脱敏目标响应证据。 */
function targetResponseEvidenceFixture({ responseIndex, requestIndex, method, surface, status }) {
  const preflight = method === "OPTIONS";
  return {
    responseIndex,
    requestIndex,
    method,
    surface,
    status,
    requestIdPresent: !preflight,
    traceIdPresent: !preflight,
    noStore: true,
    bodyPrivacyScanned: true,
    bodyJsonValid: preflight ? null : true,
    preflightHeadersValid: preflight ? true : null,
    preflightBodyValid: preflight ? true : null,
    corsOriginValid: preflight ? null : true,
    cacheAffinityVersion: null,
  };
}

/** 构造三个读 surface 均有 GET 200 的 reader 证据，并可选一条完整 OPTIONS 配对。 */
function readerTargetEvidenceFixture({ includePreflight = true } = {}) {
  const requests = [
    {
      requestIndex: 0, method: "GET", surface: "model_alias_list", requestCorsOriginValid: true,
    },
    {
      requestIndex: 1, method: "GET", surface: "request_log_list", requestCorsOriginValid: true,
    },
    {
      requestIndex: 2, method: "GET", surface: "request_log_detail", requestCorsOriginValid: true,
    },
  ];
  const responses = requests.map((request, responseIndex) => targetResponseEvidenceFixture({
    responseIndex,
    requestIndex: request.requestIndex,
    method: request.method,
    surface: request.surface,
    status: 200,
  }));
  if (includePreflight) {
    requests.push({
      requestIndex: 3,
      method: "OPTIONS",
      surface: "model_alias_list",
      requestCorsOriginValid: null,
    });
    responses.push(targetResponseEvidenceFixture({
      responseIndex: 3,
      requestIndex: 3,
      method: "OPTIONS",
      surface: "model_alias_list",
      status: 200,
    }));
  }
  return { targetRequests: requests, targetResponses: responses };
}

/** 构造已通过 Runtime 的最小脱敏状态；五个请求均代表真实 Gateway 前序证据。 */
function runtimeFixture() {
  return {
    schema_version: 1,
    status: "passed",
    prd_id: "PRD-050",
    generation: TEST_GENERATION,
    suffix: "unitrun01",
    database_file: "metahub-prd050.db",
    lifecycle: {
      credentials_cleanup_completed: true,
      credentials_cleanup_verified: true,
      credentials_changed: 10,
      mutations_pre_registered: 42,
    },
    resource_ids: {
      main_tenant_id: "tenant_unit",
      alias_id: "alias_unit",
      model_a_id: "model_unit",
      cache_affinity_id: "caf_11111111111111111111111111111111",
    },
    request_ids: {
      cache_affinity_created: "req_prd050_runtime_created_unitrun01",
      cache_affinity_pending: "req_prd050_runtime_pending_unitrun01",
      cache_affinity_warm: "req_prd050_runtime_warm_unitrun01",
      cache_affinity_rebound: "req_prd050_runtime_rebound_unitrun01",
      short_legacy: "req_prd050_runtime_short_unitrun01",
    },
    checks: {
      migration_0040_applied: true,
      admin_patch: true,
      developer_read_only: true,
      viewer_read_denied: true,
      viewer_write_denied_before_body: true,
      conflict_refresh_via_same_tenant_get: true,
      request_log_selection_safe_fields: true,
      provider_cache_tokens_not_derived_from_estimate: true,
      database_sensitive_hits: 0,
      log_sensitive_hits: 0,
      foreign_key_check_clean: true,
      integrity_check_ok: true,
      database_binding_selection_count: 5,
    },
  };
}

/** 构造 PAGE-073 RequestLog 详情，Selection 字段严格等于后端公布白名单。 */
function detailFixture({ reason, estimatedHit, cacheTokens }) {
  return {
    request_log: {
      cache_affinity_selections: [{
        cache_affinity_selection_id: `cas_${"1".repeat(32)}`,
        cache_affinity_id: `caf_${"2".repeat(32)}`,
        cache_affinity_version: 4,
        attempt_sequence: 1,
        selection_reason: reason,
        estimated_hit: estimatedHit,
        endpoint_preference_applied: false,
        target_kind: "managed",
        model_id: "model_unit",
        provider_id: "provider_unit",
        provider_code: "local_openai",
        created_at: "2026-07-21T00:00:00Z",
      }],
      usage: [{ cache_tokens: cacheTokens }],
    },
  };
}

test("runtime state requires five unique real Gateway request ids and completed cleanup", () => {
  const runtime = validateRuntimeStateDocument(runtimeFixture());
  assert.equal(runtime.generation, TEST_GENERATION);
  assert.equal(runtime.suffix, "unitrun01");
  assert.equal(runtime.databaseFile, "metahub-prd050.db");
  assert.deepEqual(Object.keys(runtime.requestIds), REQUIRED_RUNTIME_REQUEST_KEYS);
  assert.equal(new Set(Object.values(runtime.requestIds)).size, 5);

  for (const unsafe of [
    { ...runtimeFixture(), schema_version: undefined },
    { ...runtimeFixture(), schema_version: 2 },
    { ...runtimeFixture(), schema_version: true },
    { ...runtimeFixture(), status: "failed" },
    { ...runtimeFixture(), prd_id: "PRD-049" },
    { ...runtimeFixture(), generation: undefined },
    { ...runtimeFixture(), generation: "unitbatch" },
    {
      ...runtimeFixture(),
      lifecycle: { credentials_cleanup_completed: false, credentials_cleanup_verified: false },
    },
    {
      ...runtimeFixture(),
      request_ids: { ...runtimeFixture().request_ids, cache_affinity_pending: undefined },
    },
    {
      ...runtimeFixture(),
      request_ids: {
        ...runtimeFixture().request_ids,
        cache_affinity_pending: runtimeFixture().request_ids.cache_affinity_created,
      },
    },
    { ...runtimeFixture(), data_class: "classified.unit" },
    { ...runtimeFixture(), access_token: TEST_SECRETS.uiToken },
  ]) {
    assert.throws(() => validateRuntimeStateDocument(unsafe));
  }
});

test("runtime state failures never echo credential-bearing values", () => {
  let failure;
  try {
    validateRuntimeStateDocument({
      ...runtimeFixture(),
      password: TEST_SECRETS.adminPassword,
    });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof Error);
  for (const secret of Object.values(TEST_SECRETS)) {
    assert.equal(failure.message.includes(secret), false);
  }
});

test("artifact and database paths remain inside one PRD-050 batch", () => {
  const root = path.resolve("D:\\MetaHub", "artifacts", "prd050", "20260721T010203");
  assert.equal(
    resolveRuntimeDatabasePath(root, "metahub-prd050.db"),
    path.join(root, "metahub-prd050.db"),
  );
  assert.equal(pathIsInside(root, path.join(root, "browser", "result.json")), true);
  assert.equal(pathIsInside(root, root), false);
  for (const unsafe of ["../escape.db", "nested/file.db", "C:\\tmp\\escape.db", "not-db.txt"]) {
    assert.throws(() => resolveRuntimeDatabasePath(root, unsafe));
  }
});

test("acceptance roots require the runner cwd and exact physical workspace boundaries", () => {
  const existing = new Set([TEST_WORKSPACE_ROOT, TEST_ARTIFACT_ROOT]);
  // 注入闭集 exists 实现，只承认固定工作区与制品根已经存在，避免测试读取主机其他路径。
  const existsSyncImpl = (value) => existing.has(path.resolve(value));
  // 安全 realpath 映射保持逻辑路径不变，用于验证边界判定且不依赖 junction 权限。
  const safeRealpath = (value) => path.resolve(value);
  const roots = resolveAcceptanceRoots({
    workingDirectory: TEST_WORKSPACE_ROOT,
    existsSyncImpl,
    realpathImpl: safeRealpath,
  });
  assert.deepEqual(roots, TEST_ACCEPTANCE_ROOTS);
  assert.equal(Object.isFrozen(roots), true);
  // 正式 helper 只允许 process.cwd，不得通过环境变量引入第二套信任根。
  assert.doesNotMatch(resolveAcceptanceRoots.toString(), /process\\.env|METAHUB_.*(?:ROOT|DIR)/);

  assert.throws(() => resolveAcceptanceRoots({
    workingDirectory: path.join(TEST_WORKSPACE_ROOT, "artifacts"),
    existsSyncImpl,
    realpathImpl: safeRealpath,
  }));
  assert.throws(() => resolveAcceptanceRoots({
    workingDirectory: TEST_WORKSPACE_ROOT,
    existsSyncImpl,
    realpathImpl: (value) => (
      path.resolve(value) === TEST_WORKSPACE_ROOT
        ? path.resolve("D:\\outside\\workspace")
        : path.resolve(value)
    ),
  }));
  assert.throws(() => resolveAcceptanceRoots({
    workingDirectory: TEST_WORKSPACE_ROOT,
    existsSyncImpl,
    realpathImpl: (value) => (
      path.resolve(value) === TEST_ARTIFACT_ROOT
        ? path.resolve("D:\\outside\\prd050")
        : path.resolve(value)
    ),
  }));
});

test("argument parser accepts explicit loopback runtime scope and keeps password outside argv", () => {
  const parserEnv = {
    METAHUB_PRD050_ADMIN_PASSWORD: "Prd050Browser!forbidden-prd-env-secret",
    METAHUB_ADMIN_PASSWORD: "Prd050Browser!forbidden-generic-env-secret",
    METAHUB_CHROME_PATH: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  };
  // 每次解析都提供新的单行标准输入，证明管理员口令不依赖环境或 argv。
  const passwordInput = () => `${TEST_SECRETS.adminPassword}\n`;
  const validArgv = [
    "--generation", TEST_GENERATION,
    "--password-stdin",
    "--runtime-state", TEST_RUNTIME_STATE_PATH,
    "--control-url", ORIGINS.controlOrigin,
    "--gateway-url", ORIGINS.gatewayOrigin,
    "--console-url", ORIGINS.consoleOrigin,
    "--confirm-local-state-mutation",
  ];
  const args = parseBrowserArgs(
    validArgv,
    parserEnv,
    TEST_ACCEPTANCE_ROOTS,
    passwordInput,
  );
  assert.equal(args.controlOrigin, ORIGINS.controlOrigin);
  assert.equal(args.generation, TEST_GENERATION);
  assert.equal(args.adminPassword, TEST_SECRETS.adminPassword);
  assert.equal(args.workspaceRoot, TEST_WORKSPACE_ROOT);
  assert.equal(args.artifactRoot, TEST_ARTIFACT_ROOT);
  assert.equal(Object.hasOwn(parserEnv, "METAHUB_PRD050_ADMIN_PASSWORD"), false);
  assert.equal(Object.hasOwn(parserEnv, "METAHUB_ADMIN_PASSWORD"), false);
  assert.ok(args.resultPath.endsWith(path.join("browser", "browser-result.json")));

  // 重试目录必须留在同批次，且非法大小写、分隔符、连字符和超长名称全部失败关闭。
  const retryArgs = parseBrowserArgs(
    [...validArgv, "--attempt", "final_retry"],
    {},
    TEST_ACCEPTANCE_ROOTS,
    passwordInput,
  );
  assert.equal(path.basename(retryArgs.browserDirectory), "browser-final_retry");
  assert.ok(retryArgs.resultPath.endsWith(path.join("browser-final_retry", "browser-result.json")));
  for (const unsafeAttempt of ["../escape", "Final", "a-b", "a".repeat(33)]) {
    assert.throws(() => parseBrowserArgs(
      [...validArgv, "--attempt", unsafeAttempt],
      {},
      TEST_ACCEPTANCE_ROOTS,
      passwordInput,
    ));
  }

  const invalidControlArgv = [...validArgv];
  invalidControlArgv[invalidControlArgv.indexOf("--control-url") + 1] = "http://127.0.0.1:18001/path";
  assert.throws(() => parseBrowserArgs(
    invalidControlArgv,
    {},
    TEST_ACCEPTANCE_ROOTS,
    passwordInput,
  ));
  assert.throws(() => parseBrowserArgs(
    validArgv.filter((item) => item !== "--confirm-local-state-mutation"),
    {},
    TEST_ACCEPTANCE_ROOTS,
    passwordInput,
  ));

  const generationIndex = validArgv.indexOf("--generation");
  const missingGenerationArgv = [...validArgv];
  missingGenerationArgv.splice(generationIndex, 2);
  assert.throws(() => parseBrowserArgs(
    missingGenerationArgv,
    {},
    TEST_ACCEPTANCE_ROOTS,
    passwordInput,
  ));
  for (const generation of ["unitbatch", "20260723T010204"]) {
    const invalidGenerationArgv = [...validArgv];
    invalidGenerationArgv[generationIndex + 1] = generation;
    assert.throws(() => parseBrowserArgs(
      invalidGenerationArgv,
      {},
      TEST_ACCEPTANCE_ROOTS,
      passwordInput,
    ));
  }

  assert.throws(() => parseBrowserArgs(
    validArgv.filter((item) => item !== "--password-stdin"),
    {},
    TEST_ACCEPTANCE_ROOTS,
    passwordInput,
  ));
  for (const stdin of ["", `${TEST_SECRETS.adminPassword}\nextra\n`]) {
    assert.throws(() => parseBrowserArgs(
      validArgv,
      {},
      TEST_ACCEPTANCE_ROOTS,
      () => stdin,
    ));
  }
  assert.throws(() => parseBrowserArgs(
    [...validArgv, "--admin-password", "unit-forbidden-admin-password"],
    {},
    TEST_ACCEPTANCE_ROOTS,
    passwordInput,
  ));
});

test("origin normalizer rejects external hosts credentials paths query and hash", () => {
  assert.equal(normalizeLoopbackOrigin("http://localhost:18001", "control"), "http://localhost:18001");
  for (const unsafe of [
    "https://127.0.0.1:18001",
    "http://example.invalid:18001",
    "http://user:pass@127.0.0.1:18001",
    "http://127.0.0.1:18001/api",
    "http://127.0.0.1:18001/?token=value",
    "http://127.0.0.1:18001/#fragment",
  ]) {
    assert.throws(() => normalizeLoopbackOrigin(unsafe, "control"));
  }
});

test("network decision permits exact loopback services and blocks exfiltration or Gateway credentials", () => {
  assert.deepEqual(
    decideBrowserNetworkRequest({ url: `${ORIGINS.consoleOrigin}/src/app/main.js` }, ORIGINS),
    { allowed: true, destination: "console", violation: null },
  );
  assert.deepEqual(
    decideBrowserNetworkRequest({
      url: `${ORIGINS.controlOrigin}/model-aliases`,
      headers: { Origin: ORIGINS.consoleOrigin },
    }, ORIGINS),
    { allowed: true, destination: "control", violation: null },
  );
  for (const headers of [{}, { origin: "http://127.0.0.1:19999" }]) {
    assert.deepEqual(
      decideBrowserNetworkRequest({
        url: `${ORIGINS.controlOrigin}/model-aliases`,
        headers,
      }, ORIGINS),
      { allowed: false, destination: "blocked", violation: "target_origin" },
    );
  }
  assert.deepEqual(
    decideBrowserNetworkRequest({ url: `${ORIGINS.gatewayOrigin}/health` }, ORIGINS),
    { allowed: true, destination: "gateway_health", violation: null },
  );
  assert.equal(decideBrowserNetworkRequest({
    url: `${ORIGINS.gatewayOrigin}/health`,
    headers: { Authorization: `Bearer ${TEST_SECRETS.uiToken}` },
  }, ORIGINS).allowed, false);
  assert.equal(decideBrowserNetworkRequest({ url: "https://example.invalid/collect" }, ORIGINS).allowed, false);
  assert.equal(decideBrowserNetworkRequest({ url: "http://127.0.0.1:19000/health" }, ORIGINS).allowed, false);
});

test("target API classifier drops query values and identifies only PRD-050 browser surfaces", () => {
  assert.equal(
    classifyTargetApi(`${ORIGINS.controlOrigin}/model-aliases?environment_id=secret`, ORIGINS.controlOrigin),
    "model_alias_list",
  );
  assert.equal(
    classifyTargetApi(`${ORIGINS.controlOrigin}/model-aliases/alias_unit/cache-affinity`, ORIGINS.controlOrigin),
    "cache_config_write",
  );
  assert.equal(
    classifyTargetApi(`${ORIGINS.controlOrigin}/request-logs?request_id=secret`, ORIGINS.controlOrigin),
    "request_log_list",
  );
  assert.equal(
    classifyTargetApi(`${ORIGINS.controlOrigin}/request-logs/req_unit`, ORIGINS.controlOrigin),
    "request_log_detail",
  );
  assert.equal(classifyTargetApi(`${ORIGINS.gatewayOrigin}/request-logs`, ORIGINS.controlOrigin), null);
  assert.equal(classifyTargetApi("not-a-url", ORIGINS.controlOrigin), null);
});

test("DOM URL and storage privacy allows only the active authentication token slot", () => {
  const exactSecrets = collectConfiguredSecrets(Object.values(TEST_SECRETS), {});
  const expectedOwner = {
    tenantId: "tenant_unit",
    userId: "user_developer_unit",
    email: "developer@metahub.local",
    role: "developer",
    sessionId: "session_developer_unit",
  };
  const safe = {
    domText: "Prompt Cache 亲和 预计复用绑定",
    domHtml: '<section class="cache-affinity-selection-section">预计复用绑定</section>',
    url: `${ORIGINS.consoleOrigin}/#request-logs`,
    localStorageEntries: [],
    sessionStorageEntries: [
      ["metahub.auth_token", TEST_SECRETS.uiToken],
      ["metahub.auth_context", JSON.stringify({
        user: {
          tenant_id: expectedOwner.tenantId,
          user_id: expectedOwner.userId,
          email: expectedOwner.email,
          role: expectedOwner.role,
        },
        session: { session_id: expectedOwner.sessionId },
      })],
    ],
  };
  const loggedInOptions = {
    exactSecrets,
    allowedOrigins: [ORIGINS.consoleOrigin],
    activeToken: TEST_SECRETS.uiToken,
    expectedOwner,
  };
  assert.equal(assertSensitiveSurfaceSnapshot(safe, {
    ...loggedInOptions,
  }).authStorageContractValid, true);
  for (const unsafe of [
    { ...safe, domText: TEST_SECRETS.prompt },
    { ...safe, domHtml: `<main>${TEST_SECRETS.providerKey}</main>` },
    { ...safe, url: `${ORIGINS.consoleOrigin}/?token=${TEST_SECRETS.uiToken}` },
    { ...safe, localStorageEntries: [["debug", TEST_SECRETS.viewerPassword]] },
    {
      ...safe,
      sessionStorageEntries: [
        ["metahub.auth_token", TEST_SECRETS.uiToken],
        ["metahub.auth_context", TEST_SECRETS.virtualKey],
      ],
    },
  ]) {
    assert.throws(() => assertSensitiveSurfaceSnapshot(unsafe, loggedInOptions));
  }
  const staleOwner = structuredClone(safe);
  staleOwner.sessionStorageEntries[1][1] = JSON.stringify({
    user: { tenant_id: "tenant_other", user_id: expectedOwner.userId, email: expectedOwner.email, role: "developer" },
    session: { session_id: expectedOwner.sessionId },
  });
  assert.throws(() => assertSensitiveSurfaceSnapshot(staleOwner, loggedInOptions));
  assert.equal(assertSensitiveSurfaceSnapshot({
    ...safe,
    sessionStorageEntries: [],
  }, { exactSecrets, allowedOrigins: [ORIGINS.consoleOrigin], loggedOut: true }).authStorageContractValid, true);
});

test("PAGE-073 accepts pending warm rebound and created only through the exact Selection whitelist", () => {
  for (const expectation of Object.values(PAGE073_EXPECTATIONS)) {
    const cacheTokens = expectation.state === "created" ? 17 : expectation.state === "warm" ? 0 : 3;
    const projection = assertPage073DetailPayload(detailFixture({
      reason: expectation.reason,
      estimatedHit: expectation.estimatedHit,
      cacheTokens,
    }), { ...expectation, cacheTokens });
    assert.equal(projection.state, expectation.state);
    assert.equal(projection.cacheTokens, cacheTokens);
    assert.equal(projection.dtoWhitelistMatched, true);
  }

  const unsafe = detailFixture({ reason: "binding_warm_reused", estimatedHit: true, cacheTokens: 0 });
  unsafe.request_log.cache_affinity_selections[0].provider_credential_id_snapshot = "credential_unit";
  assert.throws(() => assertPage073DetailPayload(unsafe, PAGE073_EXPECTATIONS.cache_affinity_warm));
  const missing = detailFixture({ reason: "binding_warm_reused", estimatedHit: true, cacheTokens: 0 });
  delete missing.request_log.cache_affinity_selections[0].provider_code;
  assert.throws(() => assertPage073DetailPayload(missing, PAGE073_EXPECTATIONS.cache_affinity_warm));
  assert.equal(SAFE_SELECTION_FIELDS.size, 12);
});

test("estimated reuse and Provider reported cache tokens remain independent counterexamples", () => {
  assert.equal(assertCacheEvidenceIndependent({
    created: { estimatedHit: false, cacheTokens: 17 },
    warm: { estimatedHit: true, cacheTokens: 0 },
  }), true);
  assert.throws(() => assertCacheEvidenceIndependent({
    created: { estimatedHit: false, cacheTokens: 0 },
    warm: { estimatedHit: true, cacheTokens: 0 },
  }));
  assert.throws(() => assertCacheEvidenceIndependent({
    created: { estimatedHit: false, cacheTokens: 17 },
    warm: { estimatedHit: true, cacheTokens: 9 },
  }));
});

test("configuration semantics ignore CAS version but preserve all four business fields", () => {
  const original = {
    status: "enabled",
    data_class: "regulated.unit",
    minimum_prefix_chars: 1024,
    ttl_seconds: 3600,
    version: 3,
  };
  assert.deepEqual(configSemantics(original), {
    status: "enabled",
    data_class: "regulated.unit",
    minimum_prefix_chars: 1024,
    ttl_seconds: 3600,
  });
  assert.equal(configSemanticsEqual(original, { ...original, version: 99 }), true);
  assert.equal(configSemanticsEqual(original, { ...original, ttl_seconds: 300 }), false);
});

test("backend permission matrix requires admin write and viewer pre-body denial", () => {
  assert.deepEqual(backendPermissionExpectation("admin"), {
    listStatus: 422,
    listCode: "model_alias.status_invalid",
    writeStatus: 200,
    writeCode: null,
    detailStatus: 200,
    detailCode: null,
  });
  assert.deepEqual(backendPermissionExpectation("developer"), {
    listStatus: 422,
    listCode: "model_alias.status_invalid",
    writeStatus: 403,
    writeCode: "auth.permission_denied",
    detailStatus: 200,
    detailCode: null,
  });
  assert.deepEqual(backendPermissionExpectation("viewer"), {
    listStatus: 403,
    listCode: "auth.permission_denied",
    writeStatus: 403,
    writeCode: "auth.permission_denied",
    detailStatus: 403,
    detailCode: "auth.permission_denied",
  });
  assert.throws(() => backendPermissionExpectation("owner"));
  const evidence = {
    admin: { listStatus: 422, writeStatus: 200, detailStatus: 200 },
    developer: { listStatus: 422, writeStatus: 403, detailStatus: 200 },
    viewer: { listStatus: 403, writeStatus: 403, detailStatus: 403 },
  };
  assert.equal(assertBackendPermissionEvidence(evidence).backendRbacVerified, true);
  assert.throws(() => assertBackendPermissionEvidence({
    ...evidence,
    developer: { ...evidence.developer, writeStatus: 200 },
  }));
  assert.throws(() => assertBackendPermissionEvidence({
    ...evidence,
    viewer: { ...evidence.viewer, writeStatus: 422 },
  }));
});

test("role evidence requires reader pages while developer never writes and viewer never prefetches", () => {
  const reader = readerTargetEvidenceFixture();
  const developer = assertRoleTargetEvidence("developer", reader);
  assert.equal(developer.targetWriteCount, 0);
  assert.equal(developer.targetWriteResponseCount, 0);
  assert.equal(developer.targetInterruptedWriteCount, 0);
  assert.equal(developer.targetPreflightCount, 1);
  assert.deepEqual(assertRoleTargetEvidence("viewer", { targetRequests: [], targetResponses: [] }), {
    targetRequestCount: 0,
    targetResponseCount: 0,
    targetCorsResponseCount: 0,
    targetWriteCount: 0,
    targetWriteResponseCount: 0,
    targetInterruptedWriteCount: 0,
    targetPreflightCount: 0,
    targetPrefetchCount: 0,
  });
  assert.throws(() => assertRoleTargetEvidence("viewer", reader));
  assert.throws(() => assertRoleTargetEvidence("developer", {
    ...reader,
    targetRequests: [
      ...reader.targetRequests,
      {
        requestIndex: 4,
        method: "PATCH",
        surface: "cache_config_write",
        requestCorsOriginValid: true,
      },
    ],
  }));

  const adminRequests = [
    ...reader.targetRequests,
    {
      requestIndex: 4, method: "PATCH", surface: "cache_config_write", requestCorsOriginValid: true,
    },
    {
      requestIndex: 5, method: "PATCH", surface: "cache_config_write", requestCorsOriginValid: true,
    },
    {
      requestIndex: 6, method: "PATCH", surface: "cache_config_write", requestCorsOriginValid: true,
    },
    // 三条无响应 PATCH 分别对应 owner after-commit、transport abort 与 session-switch 固定流程。
    {
      requestIndex: 7, method: "PATCH", surface: "cache_config_write", requestCorsOriginValid: true,
    },
    {
      requestIndex: 8, method: "PATCH", surface: "cache_config_write", requestCorsOriginValid: true,
    },
    {
      requestIndex: 9, method: "PATCH", surface: "cache_config_write", requestCorsOriginValid: true,
    },
  ];
  const adminResponses = [
    ...reader.targetResponses,
    targetResponseEvidenceFixture({
      responseIndex: 4, requestIndex: 4, method: "PATCH", surface: "cache_config_write", status: 200,
    }),
    targetResponseEvidenceFixture({
      responseIndex: 5, requestIndex: 5, method: "PATCH", surface: "cache_config_write", status: 200,
    }),
    targetResponseEvidenceFixture({
      responseIndex: 6, requestIndex: 6, method: "PATCH", surface: "cache_config_write", status: 409,
    }),
  ];
  const adminEvidence = { targetRequests: adminRequests, targetResponses: adminResponses };
  const admin = assertRoleTargetEvidence("admin", adminEvidence);
  assert.equal(admin.targetWriteCount, 6);
  assert.equal(admin.targetWriteResponseCount, 3);
  assert.equal(admin.targetInterruptedWriteCount, 3);
  assert.equal(admin.targetPreflightCount, 1);

  assert.throws(() => assertRoleTargetEvidence("developer", {
    ...reader,
    targetResponses: reader.targetResponses.map((item) => (
      item.method === "OPTIONS" ? { ...item, preflightHeadersValid: false } : item
    )),
  }));
  assert.throws(() => assertRoleTargetEvidence("admin", {
    ...adminEvidence,
    targetResponses: adminResponses.map((item) => (
      item.status === 409 ? { ...item, status: 200 } : item
    )),
  }));
  assert.throws(() => assertRoleTargetEvidence("admin", {
    ...adminEvidence,
    targetResponses: [
      ...adminResponses,
      { ...adminResponses[0], responseIndex: 7 },
    ],
  }));
  assert.throws(() => assertRoleTargetEvidence("admin", {
    ...adminEvidence,
    targetResponses: adminResponses.map((item, index) => (
      index === 0 ? { ...item, requestIndex: 99 } : item
    )),
  }));
  assert.throws(() => assertRoleTargetEvidence("admin", {
    ...adminEvidence,
    targetRequests: adminRequests.slice(0, -1),
  }));
  assert.throws(() => assertRoleTargetEvidence("admin", {
    ...adminEvidence,
    targetRequests: [
      ...adminRequests,
      {
        requestIndex: 10,
        method: "PATCH",
        surface: "cache_config_write",
        requestCorsOriginValid: true,
      },
    ],
  }));
});

test("role evidence accepts no observable OPTIONS but requires exact ACAO evidence", () => {
  const withoutPreflight = readerTargetEvidenceFixture({ includePreflight: false });
  const result = assertRoleTargetEvidence("developer", withoutPreflight);
  assert.equal(result.targetPreflightCount, 0);
  assert.equal(result.targetCorsResponseCount, 3);
  assert.equal(result.targetWriteResponseCount, 0);
  assert.equal(result.targetInterruptedWriteCount, 0);
  assert.throws(() => assertRoleTargetEvidence("developer", {
    ...withoutPreflight,
    targetResponses: withoutPreflight.targetResponses.map((item, index) => (
      index === 0 ? { ...item, corsOriginValid: false } : item
    )),
  }));
  for (const requestPatch of [
    { requestCorsOriginValid: false },
    { requestCorsOriginValid: undefined },
  ]) {
    assert.throws(() => assertRoleTargetEvidence("developer", {
      ...withoutPreflight,
      targetRequests: withoutPreflight.targetRequests.map((item, index) => (
        index === 0 ? { ...item, ...requestPatch } : item
      )),
    }));
  }
  // 方法、surface 和状态都是闭集；即使请求/响应彼此伪造为一致也不能越过门禁。
  for (const [requestPatch, responsePatch] of [
    [{ method: "POST" }, { method: "POST" }],
    [{ surface: "unknown_surface" }, { surface: "unknown_surface" }],
    [{}, { status: 204 }],
  ]) {
    assert.throws(() => assertRoleTargetEvidence("developer", {
      targetRequests: withoutPreflight.targetRequests.map((item, index) => (
        index === 0 ? { ...item, ...requestPatch } : item
      )),
      targetResponses: withoutPreflight.targetResponses.map((item, index) => (
        index === 0 ? { ...item, ...responsePatch } : item
      )),
    }));
  }
  assert.throws(() => assertRoleTargetEvidence("developer", {
    ...withoutPreflight,
    targetResponses: withoutPreflight.targetResponses.slice(0, 2),
  }));
});

test("target preflight validates CORS headers and keeps plain OK separate from business JSON", async () => {
  const responseHeaders = {
    "access-control-allow-origin": ORIGINS.consoleOrigin,
    "access-control-allow-methods": "GET, PATCH, OPTIONS",
    "access-control-allow-headers": "Authorization, Content-Type, X-Request-ID",
    "cache-control": "no-store, private",
  };
  const requestHeaders = {
    origin: ORIGINS.consoleOrigin,
    "access-control-request-method": "PATCH",
    "access-control-request-headers": "authorization,content-type,x-request-id",
  };
  const exactOriginHeaderEntries = [
    { name: "Access-Control-Allow-Origin", value: ORIGINS.consoleOrigin },
  ];
  assert.equal(hasSingleExactResponseHeader(
    exactOriginHeaderEntries,
    "access-control-allow-origin",
    ORIGINS.consoleOrigin,
  ), true);
  assert.equal(targetCorsOriginValid({
    method: "GET",
    responseHeaderEntries: exactOriginHeaderEntries,
    consoleOrigin: ORIGINS.consoleOrigin,
  }), true);
  assert.equal(targetCorsOriginValid({
    method: "OPTIONS",
    responseHeaderEntries: exactOriginHeaderEntries,
    consoleOrigin: ORIGINS.consoleOrigin,
  }), null);
  assert.equal(targetRequestCorsOriginValid({
    method: "GET",
    requestHeaders: { Origin: ORIGINS.consoleOrigin },
    consoleOrigin: ORIGINS.consoleOrigin,
  }), true);
  assert.equal(targetRequestCorsOriginValid({
    method: "GET",
    requestHeaders: {},
    consoleOrigin: ORIGINS.consoleOrigin,
  }), false);
  assert.equal(targetRequestCorsOriginValid({
    method: "PATCH",
    requestHeaders: { origin: "http://127.0.0.1:19999" },
    consoleOrigin: ORIGINS.consoleOrigin,
  }), false);
  assert.equal(targetRequestCorsOriginValid({
    method: "OPTIONS",
    requestHeaders: {},
    consoleOrigin: ORIGINS.consoleOrigin,
  }), null);
  assert.equal(targetPreflightHeadersValid({
    method: "OPTIONS",
    status: 200,
    responseHeaders,
    responseHeaderEntries: exactOriginHeaderEntries,
    requestHeaders,
    consoleOrigin: ORIGINS.consoleOrigin,
  }), true);
  // 缺失、重复同值、重复异值、逗号折叠值和通配符都必须失败；扁平 Header 字典即使
  // 仍显示精确 origin，也不能替代 headersArray 的原始基数证据。
  const invalidOriginHeaderEntrySets = [
    [],
    [...exactOriginHeaderEntries, ...exactOriginHeaderEntries],
    [...exactOriginHeaderEntries, { name: "access-control-allow-origin", value: "http://127.0.0.1:19999" }],
    [{ name: "access-control-allow-origin", value: `${ORIGINS.consoleOrigin}, ${ORIGINS.consoleOrigin}` }],
    [{ name: "access-control-allow-origin", value: "*" }],
  ];
  for (const responseHeaderEntries of invalidOriginHeaderEntrySets) {
    assert.equal(targetCorsOriginValid({
      method: "GET",
      responseHeaderEntries,
      consoleOrigin: ORIGINS.consoleOrigin,
    }), false);
    assert.equal(targetPreflightHeadersValid({
      method: "OPTIONS",
      status: 200,
      responseHeaders,
      responseHeaderEntries,
      requestHeaders,
      consoleOrigin: ORIGINS.consoleOrigin,
    }), false);
  }
  assert.equal(targetPreflightHeadersValid({
    method: "GET",
    status: 200,
    responseHeaders,
    responseHeaderEntries: exactOriginHeaderEntries,
    requestHeaders,
    consoleOrigin: ORIGINS.consoleOrigin,
  }), null);
  assert.deepEqual(projectTargetResponseBody(
    "OK",
    "cache_config_write",
    { runtime: { tenantId: "tenant_unit", aliasId: "alias_unit" } },
    200,
    "OPTIONS",
  ), { bodyJsonValid: null, preflightBodyValid: true, cacheAffinityVersion: null });
  assert.throws(() => projectTargetResponseBody(
    "not-ok",
    "cache_config_write",
    { runtime: { tenantId: "tenant_unit", aliasId: "alias_unit" } },
    200,
    "OPTIONS",
  ));

  let releaseHeaderEntries;
  const delayedHeaderEntries = new Promise((resolve) => { releaseHeaderEntries = resolve; });
  const record = {
    responseIndex: 0,
    requestIndex: 0,
    method: "GET",
    surface: "request_log_list",
    status: 200,
    corsOriginValid: null,
    bodyPrivacyScanned: false,
    bodyJsonValid: false,
    preflightHeadersValid: null,
    preflightBodyValid: null,
    cacheAffinityVersion: null,
  };
  const evidence = {
    pendingResponseScans: new Set(),
    bodyPrivacyScanCount: 0,
    violations: [],
  };
  const response = {
    /** 正文立即可读，确保测试真正等待的是异步原始 Header。 */
    async text() { return "{}"; },
    /** 模拟 Playwright 的异步 headersArray，释放前 pending 集合不得收敛。 */
    async headersArray() { return delayedHeaderEntries; },
    headers() { return responseHeaders; },
    request() { return { headers: () => requestHeaders }; },
  };
  const responseScan = scheduleTargetResponseScan(response, record, {
    origins: ORIGINS,
    secretRegistry: new Set(),
    runtime: { tenantId: "tenant_unit", aliasId: "alias_unit" },
  }, evidence);
  assert.equal(evidence.pendingResponseScans.size, 1);
  assert.equal(record.bodyPrivacyScanned, false);
  releaseHeaderEntries(exactOriginHeaderEntries);
  await responseScan;
  assert.equal(evidence.pendingResponseScans.size, 0);
  assert.equal(record.corsOriginValid, true);
  assert.equal(record.bodyPrivacyScanned, true);
  assert.deepEqual(evidence.violations, []);
});

test("three viewport layout contract rejects overflow clipping overlap and missing panels", () => {
  assert.deepEqual(VIEWPORTS.map((item) => [item.width, item.height]), [
    [1280, 720],
    [900, 800],
    [390, 844],
  ]);
  const viewport = VIEWPORTS[2];
  const safe = {
    missing: false,
    innerWidth: 390,
    rootScrollWidth: 390,
    bodyScrollWidth: 390,
    panelScrollWidth: 370,
    panelClientWidth: 370,
    panelLeft: 10,
    panelRight: 380,
    horizontalOffenderCount: 0,
    clippedControlCount: 0,
    clippedElementCount: 0,
    overlapCount: 0,
    inspectedElementCount: 8,
  };
  assert.equal(assertLayoutMetrics(safe, viewport).horizontalOverflow, false);
  for (const unsafe of [
    { ...safe, missing: true },
    { ...safe, rootScrollWidth: 391 },
    { ...safe, panelScrollWidth: 372 },
    { ...safe, panelRight: 392 },
    { ...safe, horizontalOffenderCount: 1 },
    { ...safe, clippedControlCount: 1 },
    { ...safe, clippedElementCount: 1 },
    { ...safe, overlapCount: 1 },
  ]) {
    assert.throws(() => assertLayoutMetrics(unsafe, viewport));
  }
});

test("stage wrapper strips credential-bearing underlying error and nested multi-failure details", async () => {
  let failure;
  try {
    await runAcceptanceStage("admin.page072.error", async () => {
      // 故意把 Token/密码放入底层异常，验证公开 wrapper 不传播原始 message。
      throw new Error(`${TEST_SECRETS.uiToken}?password=${TEST_SECRETS.adminPassword}`);
    });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof BrowserAcceptanceStageError);
  assert.equal(failure.stage, "admin.page072.error");
  assert.equal(failure.message.includes(TEST_SECRETS.uiToken), false);

  const precise = new BrowserAcceptanceStageError("developer.page073.warm", "assertion_failed");
  assert.equal(toSafeStageError(new AggregateError([precise]), "browser.main"), precise);
  const safeSecondary = new BrowserAcceptanceStageError("browser.close", "browser_close_failed");
  assert.equal(
    toSafeStageError(new AggregateError([precise, safeSecondary]), "browser.main"),
    precise,
  );
  const generic = toSafeStageError(new AggregateError([
    precise,
    new Error(TEST_SECRETS.providerKey),
  ]), "browser.main", "acceptance_failed");
  assert.equal(generic.stage, "browser.main");
  assert.equal(generic.message.includes(TEST_SECRETS.providerKey), false);
});

test("owned lifecycle always runs cleanup and never returns partial success", async () => {
  let cleanupCount = 0;
  let failure;
  try {
    await runOwnedStateLifecycle({
      /** 主阶段故意泄露式失败，wrapper 必须脱敏。 */
      executeAction: async () => { throw new Error(TEST_SECRETS.providerKey); },
      /** cleanup 必须执行一次并返回安全布尔值。 */
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
    /** 成功主阶段只返回脱敏页面事实。 */
    executeAction: async () => ({ page: "passed" }),
    /** 成功 cleanup 返回最终验证状态。 */
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
      /** 页面阶段模拟零 DOM 成功。 */
      executeAction: async () => ({ zeroDom: true }),
      /** 注销故意失败，原始 Token 不得离开角色边界。 */
      logoutAction: async () => { throw new Error(TEST_SECRETS.uiToken); },
      /** 即使注销失败也必须关闭 Context。 */
      closeAction: async () => { closeCount += 1; },
    });
  } catch (error) {
    failure = error;
  }
  assert.equal(closeCount, 1);
  assert.ok(failure instanceof AggregateError);
  assert.ok(failure.errors.some((item) => item.stage === "viewer.logout"));
  assert.equal(failure.message.includes(TEST_SECRETS.uiToken), false);
});

test("browser guard final evidence settles response scans and fails every non-DOM channel closed", async () => {
  const settledEvidence = browserGuardEvidenceFixture();
  let responseScanSettled = false;
  let responseScan;
  // 模拟 Context close 前已经登记、但要到下一轮事件队列才完成的目标响应隐私扫描。
  responseScan = new Promise((resolve) => {
    setImmediate(() => {
      responseScanSettled = true;
      resolve();
    });
  }).finally(() => settledEvidence.pendingResponseScans.delete(responseScan));
  settledEvidence.pendingResponseScans.add(responseScan);
  assert.equal(await assertBrowserGuardEvidence(settledEvidence), true);
  assert.equal(responseScanSettled, true);
  assert.equal(settledEvidence.pendingResponseScans.size, 0);

  const failures = [
    [browserGuardEvidenceFixture({ blockedRequestCount: 1 }), "browser.scan.blocked_request"],
    [browserGuardEvidenceFixture({ violations: [TEST_SECRETS.uiToken] }), "browser.scan.violation"],
    [browserGuardEvidenceFixture({ browserErrors: ["console_type_error"] }),
      "browser.scan.browser_error.console_type_error"],
    [browserGuardEvidenceFixture({
      expectedTransportErrorCount: 1,
      transportErrorWindows: [{ active: true, observed: 0 }],
    }), "browser.scan.transport"],
  ];
  for (const [evidence, expectedStage] of failures) {
    await assert.rejects(
      () => assertBrowserGuardEvidence(evidence),
      (error) => error instanceof BrowserAcceptanceStageError
        && error.stage === expectedStage
        && !errorDiagnosticSurface([error]).includes(TEST_SECRETS.uiToken),
    );
  }
});

test("role lifecycle drains close events before final browser evidence and keeps clean path unchanged", async () => {
  const lateEvidence = browserGuardEvidenceFixture();
  let finalEvidenceCount = 0;
  let failure;
  try {
    await runRoleLifecycle({
      stagePrefix: "admin",
      /** 页面与注销先成功，专门验证 Context close 后到达的监听事件。 */
      executeAction: async () => ({ page: "passed" }),
      logoutAction: async () => ({ completed: true }),
      async closeAction() {
        setImmediate(() => {
          recordBrowserConsoleError(lateEvidence, {
            text: `Uncaught TypeError: ${TEST_SECRETS.uiToken}`,
            locationUrl: `${ORIGINS.consoleOrigin}/src/app/main.js`,
          }, ORIGINS);
        });
      },
      async finalEvidenceAction() {
        finalEvidenceCount += 1;
        return assertBrowserGuardEvidence(lateEvidence);
      },
    });
  } catch (error) {
    failure = error;
  }
  assert.equal(finalEvidenceCount, 1);
  assert.ok(failure instanceof AggregateError);
  assert.ok(failure.errors.some(
    (error) => error.stage === "browser.scan.browser_error.console_type_error"
      && error.code === "observed",
  ));
  assert.equal(errorDiagnosticSurface(failure.errors).includes(TEST_SECRETS.uiToken), false);

  const cleanEvidence = browserGuardEvidenceFixture();
  let cleanFinalCount = 0;
  const passed = await runRoleLifecycle({
    stagePrefix: "developer",
    executeAction: async () => ({ page: "passed" }),
    logoutAction: async () => ({ completed: true }),
    closeAction: async () => {},
    async finalEvidenceAction() {
      cleanFinalCount += 1;
      return assertBrowserGuardEvidence(cleanEvidence);
    },
  });
  assert.equal(cleanFinalCount, 1);
  assert.deepEqual(passed, { page: "passed", logout: { completed: true } });
});

test("logout retry remains bounded and never retains response body", async () => {
  let attempts = 0;
  let verifications = 0;
  const audit = await executeLogoutWithRetry({
    /** 第一次抛含 Token 异常，第二次返回 200 和不可保留的伪正文。 */
    attemptAction: async () => {
      attempts += 1;
      // 首次异常验证重试审计不保留 Token，第二次的伪正文也不得进入返回结果。
      if (attempts === 1) throw new Error(TEST_SECRETS.uiToken);
      // 返回正文只用于证明 helper 忽略非白名单字段，不代表真实 HTTP 响应。
      return { status: 200, responseBody: TEST_SECRETS.response };
    },
    /** 只有成功状态才执行终态验证。 */
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

test("deferred gate and mutation settlement preserve bounded owner state", async () => {
  const gate = createDeferred();
  let observed = false;
  const waiter = gate.promise.then(() => { observed = true; });
  gate.resolve();
  await waiter;
  assert.equal(observed, true);

  const mutation = { kind: "ui_save", intentId: "mut_1", requestId: "", outcome: "pending" };
  settleMutation(mutation, { requestId: "req_prd050_browser_save_unit", outcome: "committed" });
  assert.equal(mutation.outcome, "committed");
  assert.throws(() => settleMutation(mutation, {
    requestId: "req_prd050_browser_second_unit",
    outcome: "committed",
  }));
});

test("final result redaction rejects passwords keys internal ids classification and prompt sentinels", () => {
  const exactSecrets = Object.values(TEST_SECRETS);
  assert.equal(assertResultRedacted({
    schemaVersion: 1,
    status: "passed",
    prdId: "PRD-050",
    screenshotCount: 32,
    page073: { pending: true, warm: true, rebound: true },
  }, exactSecrets), true);
  for (const secret of exactSecrets) {
    assert.throws(() => assertResultRedacted({ status: "passed", accidental: secret }, exactSecrets));
  }
  for (const forbiddenKey of [
    "password",
    "access_token",
    "provider_credential_id",
    "cache_affinity_binding_id",
    "cache_affinity_scope_id",
    "data_class",
    "key_prefix",
    "prompt_fingerprint",
    "endpoint_revision_digest",
  ]) {
    assert.throws(() => assertResultRedacted({ status: "passed", [forbiddenKey]: "unit" }, []));
  }
});

test("URL privacy rejects data_class secret keys hashes values and foreign origins", () => {
  assert.equal(assertUrlPrivacy(`${ORIGINS.consoleOrigin}/#routing-policies`, {
    allowedOrigins: [ORIGINS.consoleOrigin],
    exactSecrets: Object.values(TEST_SECRETS),
    forbiddenValues: ["regulated.unit"],
  }).urlSensitiveHitCount, 0);
  for (const unsafe of [
    `${ORIGINS.consoleOrigin}/?data_class=regulated.unit`,
    `${ORIGINS.consoleOrigin}/#data_class=regulated.unit`,
    `${ORIGINS.consoleOrigin}/?mode=${encodeURIComponent(TEST_SECRETS.uiToken)}`,
    `${ORIGINS.consoleOrigin}/regulated.unit`,
    "http://127.0.0.1:19999/#routing-policies",
  ]) {
    assert.throws(() => assertUrlPrivacy(unsafe, {
      allowedOrigins: [ORIGINS.consoleOrigin],
      exactSecrets: Object.values(TEST_SECRETS),
      forbiddenValues: ["regulated.unit"],
    }));
  }
});

test("physical path contract rejects a lexical child whose realpath escapes the batch", () => {
  const root = path.resolve("D:\\MetaHub\\artifacts\\prd050");
  const batch = path.join(root, "unitbatch");
  const candidate = path.join(batch, "browser", "result.json");
  const existing = new Set([root, batch]);
  // 注入式 exists/realpath 闭包避免单测依赖 Windows 创建 junction 的系统权限。
  const existsSyncImpl = (value) => existing.has(path.resolve(value));
  // 安全映射保持词法路径与物理路径一致。
  const safeRealpath = (value) => path.resolve(value);
  assert.equal(assertPhysicalPathInside(batch, candidate, {
    existsSyncImpl,
    realpathImpl: safeRealpath,
  }), candidate);
  // 逃逸映射模拟 batch junction 指向工作区外，helper 必须拒绝该物理目标。
  const escapedRealpath = (value) => (
    path.resolve(value) === batch ? path.resolve("D:\\outside\\unitbatch") : path.resolve(value)
  );
  assert.throws(() => assertPhysicalPathInside(root, candidate, {
    existsSyncImpl,
    realpathImpl: escapedRealpath,
  }));
});

test("admin owner recovery waits for the real dashboard title under the admin page stage", () => {
  const acceptanceSource = readFileSync(
    new URL("../../../scripts/acceptance/prd050-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  const dashboardSource = readFileSync(
    new URL("../../src/features/dashboard/renderDashboardPanel.js", import.meta.url),
    "utf8",
  );
  const adminStart = acceptanceSource.indexOf("async function runAdminRole");
  const developerStart = acceptanceSource.indexOf("async function runDeveloperRole");
  assert.ok(adminStart >= 0 && developerStart > adminStart);
  const adminRoleSource = acceptanceSource.slice(adminStart, developerStart);

  // Dashboard 的 loading/成功/失败三态都使用同一个真实标题，验收器不得等待不存在的历史 ID。
  assert.equal((dashboardSource.match(/id="dashboard-title"/g) ?? []).length, 3);
  assert.equal(acceptanceSource.includes('navigate(page, "overview", "#overview-title")'), false);
  assert.ok(acceptanceSource.includes('overview: "#dashboard-title"'));
  assert.ok(acceptanceSource.includes("NAVIGATION_TARGET_SELECTORS[navigation]"));
  assert.ok(adminRoleSource.includes('const role = "admin"'));
  assert.ok(adminRoleSource.includes("stagePrefix: role"));
  assert.ok(adminRoleSource.includes("verifyAdminConfigStates"));
  assert.ok(acceptanceSource.includes('await navigate(page, "overview");'));
  assert.ok(acceptanceSource.includes('addFailure(error, `${stagePrefix}.page`, "page_failed");'));
});

test("state screenshots focus the requested viewport surface instead of reusing a full page image", () => {
  const acceptanceSource = readFileSync(
    new URL("../../../scripts/acceptance/prd050-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  const capturePageStart = acceptanceSource.indexOf("async function capturePage");
  const captureStateStart = acceptanceSource.indexOf("async function captureViewportState");
  const matrixStart = acceptanceSource.indexOf("function assertViewportStateMatrix");
  assert.ok(capturePageStart >= 0 && captureStateStart > capturePageStart && matrixStart > captureStateStart);
  const capturePageSource = acceptanceSource.slice(capturePageStart, captureStateStart);
  const captureStateSource = acceptanceSource.slice(captureStateStart, matrixStart);

  // empty 与 configured 同时存在于 PAGE-072；只有按各自 selector 聚焦 viewport 才能形成互异视觉证据。
  assert.ok(capturePageSource.includes("focusSelector"));
  assert.ok(capturePageSource.includes("page.locator(focusSelector)"));
  assert.ok(
    capturePageSource.includes("scrollIntoViewIfNeeded")
      || (capturePageSource.includes("focus.evaluate") && capturePageSource.includes("window.scrollTo")),
  );
  assert.equal(capturePageSource.includes("fullPage: true"), false);
  assert.ok(capturePageSource.includes("fullPage: false"));
  assert.ok(captureStateSource.includes("screenshotFocusSelector = panelSelector"));
  assert.match(captureStateSource, /capturePage\([\s\S]{0,300}screenshotFocusSelector/);
  assert.match(
    acceptanceSource,
    /"configured", "\.routing-policies-panel", scan, viewportMatrix,\s*configuredSelector/,
  );
  assert.ok(acceptanceSource.includes("screenshotHashes: new Set()"));
  assert.ok(capturePageSource.includes("state.screenshotHashes.has(screenshotHash)"));
  assert.ok(capturePageSource.includes("state.screenshotHashes.add(screenshotHash)"));
  assert.ok(capturePageSource.includes("sha256: screenshotHash"));
  assert.ok(capturePageSource.includes("bytes: screenshotBytes"));
  assert.ok(acceptanceSource.includes("assert.equal(body.schema_version, 1)"));
  assert.ok(acceptanceSource.includes("schemaVersion: 1"));
  assert.ok(acceptanceSource.includes("generation: args.generation"));
  assert.ok(acceptanceSource.includes("state.screenshotHashes.clear()"));
});

test("developer reader capture reuses the settled PAGE-072 DOM and exposes fixed failure stages", () => {
  const acceptanceSource = readFileSync(
    new URL("../../../scripts/acceptance/prd050-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  const readerStart = acceptanceSource.indexOf("async function captureReaderPages");
  const roleEvidenceStart = acceptanceSource.indexOf("function assertRoleTargetEvidence");
  assert.ok(readerStart >= 0 && roleEvidenceStart > readerStart);
  const readerSource = acceptanceSource.slice(readerStart, roleEvidenceStart);

  // developer 已在调用方等待只读行；只有不截图 PAGE-072 的 admin 流程才能执行返回路由页的导航。
  assert.match(
    readerSource,
    /if \(!includePage072\) \{[\s\S]*?navigate\(page, "routing-policies"\)[\s\S]*?\}\s*if \(includePage072\)/,
  );
  assert.match(
    readerSource,
    /browser\.reader\.\$\{role\}\.page072\.ready[\s\S]*?waitForCacheConfigRow/,
  );
  assert.match(readerSource, /\$\{stageRoot\}\.layout/);
  assert.match(readerSource, /\$\{stageRoot\}\.capture/);
});

test("screenshot role annotation keeps privacy masks while making the RBAC evidence distinct", () => {
  const adminStyle = screenshotRoleStyle("admin");
  const developerStyle = screenshotRoleStyle("developer");
  const viewerStyle = screenshotRoleStyle("viewer");
  assert.match(adminStyle, /content: "role: admin"/);
  assert.match(developerStyle, /content: "role: developer"/);
  assert.match(viewerStyle, /content: "role: viewer"/);
  assert.notEqual(adminStyle, developerStyle);
  assert.notEqual(developerStyle, viewerStyle);
  assert.match(adminStyle, /position: fixed/);
  assert.match(adminStyle, /pointer-events: none/);
  assert.throws(() => screenshotRoleStyle("finance"));
});

test("page route fallthrough calls fallback and never continue", async () => {
  let fallbackCount = 0;
  let continueCount = 0;
  await fallThroughBrowserRoute({
    fallback: async () => { fallbackCount += 1; },
    continue: async () => { continueCount += 1; },
  });
  assert.equal(fallbackCount, 1);
  assert.equal(continueCount, 0);
  await assert.rejects(() => fallThroughBrowserRoute({ continue: async () => {} }));

  const terminalOrder = [];
  const handlerSettled = createDeferred();
  const terminalPromise = settleBrowserRouteBeforeUnroute({
    async response() {
      terminalOrder.push("response");
      return { status: () => 200 };
    },
  }, handlerSettled.promise, async () => {
    terminalOrder.push("unroute");
  });
  assert.deepEqual(terminalOrder, []);
  terminalOrder.push("handlerSettled");
  handlerSettled.resolve();
  const terminalStatus = await terminalPromise;
  assert.equal(terminalStatus, 200);
  assert.deepEqual(terminalOrder, ["handlerSettled", "response", "unroute"]);

  const failedOrder = [];
  const failedHandlerSettled = Promise.resolve().then(() => {
    failedOrder.push("handlerSettled");
  });
  assert.equal(await settleBrowserRouteBeforeUnroute({
    async response() {
      failedOrder.push("requestfailed");
      return null;
    },
  }, failedHandlerSettled, async () => {
    failedOrder.push("unroute");
  }), null);
  assert.deepEqual(failedOrder, ["handlerSettled", "requestfailed", "unroute"]);

  const supervisor = createBrowserRouteFailureSupervisor();
  const neverObserved = createDeferred();
  const failureWait = supervisor.waitForSignal(
    neverObserved.promise,
    "browser.route.unit.signal",
    1_000,
  );
  let abortCount = 0;
  const supervised = superviseBrowserRoute(
    supervisor,
    "browser.route.unit",
    async () => {
      // 原异常同时模拟凭据和底层正文，监督器只能公开固定 stage/code。
      throw new Error("Bearer unit-private-token-must-not-escape");
    },
  );
  await supervised({
    async abort() {
      abortCount += 1;
      // 二次 abort 故障模拟底层 URL 泄漏风险，安全 wrapper 必须继续吞掉它。
      throw new Error("https://private.example.invalid/internal");
    },
  });
  assert.equal(abortCount, 1);
  await assert.rejects(
    failureWait,
    (error) => error instanceof BrowserAcceptanceStageError
      && error.message === "browser_acceptance_failed stage=browser.route.unit code=route_handler_failed"
      && !error.message.includes("Bearer")
      && !error.message.includes("private.example"),
  );
  await supervisor.waitForQuiescence();
  assert.throws(() => supervisor.throwIfFailed(), /stage=browser\.route\.unit/);

  const routeState = {
    origins: {
      consoleOrigin: "http://127.0.0.1:15173",
      controlOrigin: "http://127.0.0.1:18001",
      gatewayOrigin: "http://127.0.0.1:18000",
    },
    runtime: { aliasId: "alias-unit" },
    secretRegistry: new Set(["unit-private-token"]),
    forbiddenUrlValues: ["regulated.unit"],
  };
  // 虚构 Request 只验证 route.fetch 前的纯门禁，不连接本地或外部网络。
  const guardedRequest = (url, method = "PATCH") => ({
    url: () => url,
    method: () => method,
    headers: () => ({
      authorization: "Bearer opaque-unit-token",
      origin: routeState.origins.consoleOrigin,
    }),
  });
  assert.equal(assertGuardedRouteFetchRequest(
    guardedRequest("http://127.0.0.1:18001/model-aliases/alias-unit/cache-affinity"),
    routeState,
  ), true);
  assert.deepEqual(GUARDED_ROUTE_FETCH_OPTIONS, { timeout: 20_000, maxRedirects: 0 });
  assert.throws(() => assertGuardedRouteFetchRequest(
    guardedRequest(
      "http://127.0.0.1:18001/model-aliases/alias-unit/cache-affinity?access_token=unit-private-token",
    ),
    routeState,
  ));
  assert.throws(() => assertGuardedRouteFetchRequest(
    guardedRequest(
      "http://127.0.0.1:18001/model-aliases/alias-unit/cache-affinity?mode=unit",
    ),
    routeState,
  ));
  assert.throws(() => assertGuardedRouteFetchRequest(
    guardedRequest("https://outside.example.invalid/model-aliases/alias-unit/cache-affinity"),
    routeState,
  ));
  assert.throws(() => assertGuardedRouteFetchRequest(
    guardedRequest("http://127.0.0.1:18001/model-aliases/alias-unit/cache-affinity", "GET"),
    routeState,
  ));
  assert.throws(() => assertGuardedRouteFetchRequest(
    guardedRequest("http://127.0.0.1:18001/model-aliases/other/cache-affinity"),
    routeState,
  ));
});

test("route settlement is bounded and never unroutes an unconverged request", async () => {
  let unrouteCount = 0;
  const never = new Promise(() => {});
  await assert.rejects(
    settleBrowserRouteBeforeUnroute(
      { async response() { return null; } },
      never,
      async () => { unrouteCount += 1; },
      5,
    ),
    (error) => error.stage === "browser.route.settlement" && error.code === "timeout",
  );
  assert.equal(unrouteCount, 0, "handler 未收敛时不得强制 unroute");

  await assert.rejects(
    settleBrowserRouteBeforeUnroute(
      { async response() { return never; } },
      Promise.resolve(),
      async () => { unrouteCount += 1; },
      5,
    ),
    /stage=browser\.route\.settlement code=timeout/,
  );
  assert.equal(unrouteCount, 0, "Request 未收敛时不得强制 unroute");

  await assert.rejects(
    settleBrowserRouteBeforeUnroute(
      { async response() { throw new Error(TEST_SECRETS.uiToken); } },
      Promise.resolve(),
      async () => { unrouteCount += 1; },
      20,
    ),
    (error) => error.code === "operation_failed"
      && !error.message.includes(TEST_SECRETS.uiToken),
  );
  assert.equal(unrouteCount, 0, "Request 失败时不得强制 unroute");

  await assert.rejects(
    settleBrowserRouteBeforeUnroute(
      { async response() { return null; } },
      Promise.resolve(),
      () => never,
      5,
    ),
    /stage=browser\.route\.unroute code=timeout/,
  );
});

test("gate waits are lazy and consume route failures that happen before the caller waits", async () => {
  const state = {
    origins: ORIGINS,
    runtime: { aliasId: "alias-unit" },
    secretRegistry: new Set([TEST_SECRETS.uiToken]),
    forbiddenUrlValues: ["regulated.unit"],
  };
  /** 构造只记录 route/unroute 调用的内存 Page，不连接任何真实网络。 */
  const createPage = () => {
    const record = { handler: null, unrouteCount: 0 };
    return {
      record,
      async route(_pattern, handler) { record.handler = handler; },
      async unroute() { record.unrouteCount += 1; },
    };
  };
  /** 构造精确 CacheAffinity PATCH Request；response 固定为失败终态 null。 */
  const request = (requestId) => ({
    url: () => `${ORIGINS.controlOrigin}/model-aliases/alias-unit/cache-affinity`,
    method: () => "PATCH",
    headers: () => ({ "x-request-id": requestId }),
    async response() { return null; },
  });
  const unhandled = [];
  /** 记录本测试窗口内的游离 rejection；最终必须保持空数组。 */
  const onUnhandled = (error) => unhandled.push(error);
  process.on("unhandledRejection", onUnhandled);
  try {
    const beforeSupervisor = createBrowserRouteFailureSupervisor();
    const beforePage = createPage();
    const beforeGate = await installBeforeSendPatchGate(
      beforePage,
      state,
      beforeSupervisor,
      { requestId: "", outcome: "pending" },
    );
    assert.equal(typeof beforeGate.waitObserved, "function");
    await beforePage.record.handler({
      request: () => request(""),
      async abort() {},
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
    await assert.rejects(
      beforeGate.waitObserved(),
      /stage=browser\.route\.before_patch code=route_handler_failed/,
    );
    assert.equal(await beforeGate.dispose(), null);
    assert.equal(beforePage.record.unrouteCount, 1);

    const commitSupervisor = createBrowserRouteFailureSupervisor();
    const commitPage = createPage();
    const commitGate = await installAfterCommitPatchGate(
      commitPage,
      state,
      commitSupervisor,
      { requestId: "", outcome: "pending" },
    );
    assert.equal(typeof commitGate.waitCommitted, "function");
    await commitPage.record.handler({
      request: () => request("req_prd050_gate_lazy_unit"),
      async fetch(options) {
        assert.deepEqual(options, GUARDED_ROUTE_FETCH_OPTIONS);
        // 故意在真实 fetch 位置抛含 Secret 原错，验证 committed waiter 只收到固定首错。
        throw new Error(TEST_SECRETS.providerKey);
      },
      async abort() {},
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
    await assert.rejects(
      commitGate.waitCommitted(),
      (error) => error.stage === "browser.route.after_commit"
        && !error.message.includes(TEST_SECRETS.providerKey),
    );
    assert.equal(await commitGate.dispose(), null);
    assert.equal(commitPage.record.unrouteCount, 1);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

test("observed response helper consumes a late waiter rejection before restoring action failure", async () => {
  const unhandled = [];
  /** 收集全局 rejection 事件，证明 delayed waiter 已在创建时被 outcome observer 接管。 */
  const onUnhandled = (error) => unhandled.push(error);
  process.on("unhandledRejection", onUnhandled);
  try {
    const delayedFailure = new Promise((resolve, reject) => {
      setImmediate(() => reject(new Error(TEST_SECRETS.uiToken)));
    });
    const outcome = observePromiseOutcome(delayedFailure);
    await assert.rejects(
      runBrowserActionWithObservedResponse(
        async () => { throw new Error(TEST_SECRETS.providerKey); },
        outcome,
      ),
      (error) => error.message === TEST_SECRETS.providerKey,
    );
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

test("released page gate falls through the context guard and converges on one real PATCH response", async () => {
  const serviceRequests = [];
  const serviceObserved = createDeferred();
  const server = createServer((request, response) => {
    // 本地临时服务只记录固定合同字段，不保存请求正文，避免单测引入新的敏感信息表面。
    request.resume();
    request.once("end", () => {
      serviceRequests.push({
        method: request.method,
        pathname: new URL(request.url ?? "/", "http://127.0.0.1").pathname,
        requestId: String(request.headers["x-request-id"] ?? ""),
      });
      serviceObserved.resolve();
      response.writeHead(200, {
        "connection": "close",
        "content-type": "application/json; charset=utf-8",
      });
      response.end('{"status":"ok"}');
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });

  let networkTask = Promise.resolve();
  try {
    const address = server.address();
    assert.ok(address && typeof address === "object");
    const controlOrigin = `http://127.0.0.1:${address.port}`;
    const targetUrl = `${controlOrigin}/model-aliases/alias-unit/cache-affinity`;
    const requestId = "req_prd050_gate_chain_unit";
    const state = {
      origins: { ...ORIGINS, controlOrigin },
      runtime: { aliasId: "alias-unit" },
      secretRegistry: new Set([TEST_SECRETS.uiToken]),
      forbiddenUrlValues: ["regulated.unit"],
    };
    const responseSignal = createDeferred();
    const requestResponseSignal = createDeferred();
    const routeCounts = {
      pageFallback: 0,
      pageAbort: 0,
      contextHandler: 0,
      contextContinue: 0,
      contextAbort: 0,
      unroute: 0,
    };
    const requestHeaders = {
      "content-type": "application/json",
      origin: state.origins.consoleOrigin,
      "x-request-id": requestId,
    };
    const browserRequest = {
      url: () => targetUrl,
      method: () => "PATCH",
      headers: () => requestHeaders,
      response: () => requestResponseSignal.promise,
    };
    const supervisor = createBrowserRouteFailureSupervisor();

    // 复刻真实 Context 守卫：page.route 只能 fallback 到这里，由这里完成 origin 判定并唯一 continue。
    const contextHandler = superviseBrowserRoute(
      supervisor,
      "browser.route.admin.context_unit",
      async (route) => {
        routeCounts.contextHandler += 1;
        const guardedRequest = route.request();
        const decision = decideBrowserNetworkRequest({
          url: guardedRequest.url(),
          method: guardedRequest.method(),
          headers: guardedRequest.headers(),
        }, state.origins);
        assert.deepEqual(decision, { allowed: true, destination: "control", violation: null });
        await route.continue();
      },
    );
    const contextRoute = {
      request: () => browserRequest,
      async continue() {
        routeCounts.contextContinue += 1;
        // continue 只启动真实网络任务并立即交还控制权，用来复刻 Playwright terminal 早于 response 的时序。
        networkTask = fetch(targetUrl, {
          method: "PATCH",
          headers: requestHeaders,
          body: '{"enabled":true}',
        }).then(async (networkResponse) => {
          await networkResponse.text();
          const browserResponse = {
            url: () => targetUrl,
            status: () => networkResponse.status,
            request: () => browserRequest,
          };
          requestResponseSignal.resolve(browserResponse);
          responseSignal.resolve(browserResponse);
        }).catch((error) => {
          // Playwright 的 Request.response() 在请求失败时返回 null；waitForResponse rejection 已由 outcome observer 接管。
          requestResponseSignal.resolve(null);
          responseSignal.reject(error);
        });
      },
      async abort() { routeCounts.contextAbort += 1; },
    };
    const pageRecord = { handler: null, pattern: null };
    const page = {
      async route(pattern, handler) {
        pageRecord.pattern = pattern;
        pageRecord.handler = handler;
      },
      async unroute(pattern, handler) {
        assert.equal(pattern, pageRecord.pattern);
        assert.equal(handler, pageRecord.handler);
        routeCounts.unroute += 1;
      },
      waitForResponse(predicate) {
        return responseSignal.promise.then((response) => {
          assert.equal(predicate(response), true);
          return response;
        });
      },
    };
    const mutation = { requestId: "", outcome: "pending" };
    const gate = await installBeforeSendPatchGate(page, state, supervisor, mutation);
    assert.equal(typeof pageRecord.handler, "function");
    const pageRoute = {
      request: () => browserRequest,
      async fallback() {
        routeCounts.pageFallback += 1;
        await contextHandler(contextRoute);
      },
      async abort() { routeCounts.pageAbort += 1; },
    };
    const responseOutcome = observePromiseOutcome(page.waitForResponse((response) => (
      response.url() === targetUrl && response.request().method() === "PATCH"
    )));
    const action = runBrowserActionWithObservedResponse(
      () => pageRecord.handler(pageRoute),
      responseOutcome,
    );
    await gate.waitObserved();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(serviceRequests.length, 0, "gate 释放前真实服务不得收到 PATCH");

    gate.release();
    const response = await action;
    await serviceObserved.promise;
    assert.equal(response.status(), 200);
    assert.deepEqual(serviceRequests, [{
      method: "PATCH",
      pathname: "/model-aliases/alias-unit/cache-affinity",
      requestId,
    }]);
    assert.equal(mutation.requestId, requestId);

    // 两次 dispose 必须复用同一收敛 Promise；卸载不能再次 fallback、continue、abort 或重放 PATCH。
    const firstDisposal = gate.dispose();
    const secondDisposal = gate.dispose();
    assert.equal(firstDisposal, secondDisposal);
    assert.deepEqual(await Promise.all([firstDisposal, secondDisposal]), [200, 200]);
    await networkTask;
    assert.deepEqual(routeCounts, {
      pageFallback: 1,
      pageAbort: 0,
      contextHandler: 1,
      contextContinue: 1,
      contextAbort: 0,
      unroute: 1,
    });
    assert.doesNotThrow(() => supervisor.throwIfFailed());
  } finally {
    await networkTask;
    await new Promise((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }
});

test("gate dispose closes local admission before unroute and preserves the primary request", async () => {
  const state = {
    origins: ORIGINS,
    runtime: { aliasId: "alias-unit" },
    secretRegistry: new Set([TEST_SECRETS.uiToken]),
    forbiddenUrlValues: ["regulated.unit"],
  };
  const unrouteRelease = createDeferred();
  const record = {
    handler: null,
    unrouteStarted: false,
    unrouteCount: 0,
  };
  const page = {
    /** 保存真实 installer 生成的 callback，测试在 dispose 窗口主动注入迟到 Route。 */
    async route(_pattern, handler) { record.handler = handler; },
    /** 模拟 Playwright 卸载已启动但尚未返回，dispose 必须把该 Promise 纳入 active 账本。 */
    async unroute() {
      record.unrouteStarted = true;
      record.unrouteCount += 1;
      await unrouteRelease.promise;
    },
  };
  const supervisor = createBrowserRouteFailureSupervisor();
  const mutation = { requestId: "", outcome: "pending" };
  const gate = await installBeforeSendPatchGate(page, state, supervisor, mutation);
  let primaryRequestReadCount = 0;
  let primaryFallbackCount = 0;
  const primaryRequest = {
    url: () => ORIGINS.controlOrigin + "/model-aliases/alias-unit/cache-affinity",
    method: () => "PATCH",
    headers: () => ({ "x-request-id": "req_prd050_gate_primary_unit" }),
    async response() { return { status: () => 200 }; },
  };
  const primaryCallback = record.handler({
    request() {
      primaryRequestReadCount += 1;
      return primaryRequest;
    },
    async fallback() { primaryFallbackCount += 1; },
    async abort() { throw new Error("主请求不应被中断"); },
  });
  await gate.waitObserved();

  let disposalSettled = false;
  const disposal = gate.dispose().then((status) => {
    disposalSettled = true;
    return status;
  });
  let lateBusinessReadCount = 0;
  let lateTerminalCount = 0;
  // dispose 已同步 stopAccepting，但真正 unroute 尚需等待主 callback；此处精确注入旧 TOCTOU 窗口。
  await record.handler({
    request() {
      lateBusinessReadCount += 1;
      // 故意携带 Secret 的异常用于证明停收分支根本不会读取 Request，也不会把原错交给公开边界。
      throw new Error(TEST_SECRETS.providerKey);
    },
    async fallback() { lateTerminalCount += 1; },
    async continue() { lateTerminalCount += 1; },
    async abort() { lateTerminalCount += 1; },
    async fulfill() { lateTerminalCount += 1; },
  });
  await primaryCallback;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(primaryRequestReadCount, 1);
  assert.equal(primaryFallbackCount, 1);
  assert.equal(lateBusinessReadCount, 0, "停收后的 callback 不得读取 Request 或进入业务 handler");
  assert.equal(lateTerminalCount, 0, "迟到 callback 的 terminal 只能由 page.unroute 统一推进");
  assert.equal(record.unrouteStarted, true);
  assert.equal(disposalSettled, false, "page.unroute 未返回前 dispose 不得提前收敛");
  unrouteRelease.resolve();
  assert.equal(await disposal, 200);
  assert.equal(record.unrouteCount, 1);
  assert.doesNotThrow(() => supervisor.throwIfFailed());
});

test("route supervisor never performs a second terminal action after terminal has started", async () => {
  for (const terminalName of ["fallback", "continue", "fulfill"]) {
    const supervisor = createBrowserRouteFailureSupervisor();
    let terminalCount = 0;
    let abortCount = 0;
    const callback = superviseBrowserRoute(
      supervisor,
      `browser.route.${terminalName}_unit`,
      async (route) => {
        await route[terminalName]();
        // terminal 已成功开始后再抛含 Secret 异常，wrapper 不得对同一 Route 二次 abort。
        throw new Error(`${TEST_SECRETS.uiToken}-${terminalName}`);
      },
    );
    await callback({
      async [terminalName]() { terminalCount += 1; },
      async abort() { abortCount += 1; },
    });
    assert.equal(terminalCount, 1);
    assert.equal(abortCount, 0);
    assert.throws(() => supervisor.throwIfFailed(), (error) => (
      error.stage === `browser.route.${terminalName}_unit`
        && !error.message.includes(TEST_SECRETS.uiToken)
    ));
  }

  const rejectedTerminalSupervisor = createBrowserRouteFailureSupervisor();
  let rejectedAbortCount = 0;
  const rejectedTerminal = superviseBrowserRoute(
    rejectedTerminalSupervisor,
    "browser.route.fallback_rejected_unit",
    async (route) => route.fallback(),
  );
  await rejectedTerminal({
    async fallback() { throw new Error(TEST_SECRETS.providerKey); },
    async abort() { rejectedAbortCount += 1; },
  });
  assert.equal(rejectedAbortCount, 0);
  assert.throws(() => rejectedTerminalSupervisor.throwIfFailed(), /fallback_rejected_unit/);
});

test("route supervisor tracks active and sealed callbacks until their terminal work ends", async () => {
  const supervisor = createBrowserRouteFailureSupervisor();
  const activeGate = createDeferred();
  let businessHandlerCount = 0;
  const callback = superviseBrowserRoute(
    supervisor,
    "browser.route.quiescence_unit",
    async (route) => {
      businessHandlerCount += 1;
      await activeGate.promise;
      await route.continue();
    },
  );
  const activeCallback = callback({
    async continue() {},
    async abort() {},
  });
  await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(
    supervisor.waitForQuiescence(5),
    (error) => error.stage === "browser.route.quiescence" && error.code === "timeout",
  );
  activeGate.resolve();
  await activeCallback;
  await supervisor.waitForQuiescence();

  const teardownGate = createDeferred();
  let teardownStarted = false;
  const trackedTeardown = supervisor.trackOperation(() => {
    teardownStarted = true;
    return teardownGate.promise;
  });
  assert.equal(teardownStarted, true, "受控 teardown 必须在当前 JS turn 启动");
  await assert.rejects(supervisor.waitForQuiescence(5), /code=timeout/);
  teardownGate.resolve();
  await trackedTeardown;
  await supervisor.waitForQuiescence();

  supervisor.seal();
  const sealedAbortGate = createDeferred();
  let sealedAbortCount = 0;
  const sealedCallback = callback({
    async abort() {
      sealedAbortCount += 1;
      await sealedAbortGate.promise;
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(businessHandlerCount, 1, "封口后的 callback 不得进入业务 handler");
  await assert.rejects(supervisor.waitForQuiescence(5), /code=timeout/);
  sealedAbortGate.resolve();
  await sealedCallback;
  await supervisor.waitForQuiescence();
  assert.equal(sealedAbortCount, 1);
  let lateTeardownCount = 0;
  await supervisor.trackOperation(async () => { lateTeardownCount += 1; });
  assert.equal(lateTeardownCount, 0, "seal 后迟到 teardown 不得启动新副作用");
  assert.doesNotThrow(() => supervisor.throwIfFailed());
});

test("route failure signal is bounded, first-wins, and published after best-effort abort", async () => {
  const timeoutSupervisor = createBrowserRouteFailureSupervisor();
  const missingSignal = createDeferred();
  await assert.rejects(
    timeoutSupervisor.waitForSignal(missingSignal.promise, "browser.route.missing_gate", 5),
    (error) => error.stage === "browser.route.missing_gate" && error.code === "timeout",
  );

  const supervisor = createBrowserRouteFailureSupervisor();
  const observed = createDeferred();
  let abortFinished = false;
  const signalWait = supervisor.waitForSignal(
    observed.promise,
    "browser.route.before_observed_unit",
    1_000,
  );
  const callback = superviseBrowserRoute(
    supervisor,
    "browser.route.before_observed_unit",
    async () => { throw new Error(TEST_SECRETS.uiToken); },
  );
  await callback({
    async abort() {
      await new Promise((resolve) => setImmediate(resolve));
      abortFinished = true;
    },
  });
  await assert.rejects(signalWait, (error) => (
    abortFinished
      && error.stage === "browser.route.before_observed_unit"
      && !error.message.includes(TEST_SECRETS.uiToken)
  ));
  supervisor.fail("browser.route.later_failure_unit");
  assert.throws(
    () => supervisor.throwIfFailed(),
    (error) => error.stage === "browser.route.before_observed_unit",
  );
  const lateSignal = createDeferred();
  await assert.rejects(
    supervisor.waitForSignal(lateSignal.promise, "browser.route.late_waiter_unit", 1_000),
    (error) => error.stage === "browser.route.before_observed_unit",
  );
});

test("role lifecycle preserves route and close failures and drains sealed callbacks", async () => {
  const supervisor = createBrowserRouteFailureSupervisor();
  const neverObserved = createDeferred();
  const closeAbortGate = createDeferred();
  const order = [];
  let primaryCallbackSettled = false;
  let sealedCallbackSettled = false;
  let sealedBusinessHandlerCount = 0;

  const primaryCallback = superviseBrowserRoute(
    supervisor,
    "browser.route.lifecycle_unit",
    async () => { throw new Error(TEST_SECRETS.uiToken); },
  );
  const sealedCallback = superviseBrowserRoute(
    supervisor,
    "browser.route.sealed_lifecycle_unit",
    async () => { sealedBusinessHandlerCount += 1; },
  );
  let failure;
  try {
    await runRoleLifecycle({
      stagePrefix: "admin",
      routeSupervisor: supervisor,
      async executeAction() {
        void primaryCallback({
          async abort() { order.push("primary_abort"); },
        }).then(() => { primaryCallbackSettled = true; });
        await supervisor.waitForSignal(
          neverObserved.promise,
          "browser.route.lifecycle_gate_unit",
          1_000,
        );
        return { unreachable: true };
      },
      async logoutAction() {
        order.push("logout");
        return { completed: true };
      },
      async closeAction() {
        order.push("close");
        void sealedCallback({
          async abort() {
            order.push("sealed_abort_start");
            await closeAbortGate.promise;
            order.push("sealed_abort_end");
          },
        }).then(() => { sealedCallbackSettled = true; });
        setImmediate(() => closeAbortGate.resolve());
        // 故意让 Context close 同时失败，验证 route 首错与关闭错误都被脱敏保留。
        throw new Error(TEST_SECRETS.providerKey);
      },
    });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof AggregateError);
  assert.equal(primaryCallbackSettled, true);
  assert.equal(sealedCallbackSettled, true);
  assert.equal(sealedBusinessHandlerCount, 0);
  assert.ok(failure.errors.some((error) => error.stage === "browser.route.lifecycle_unit"));
  assert.ok(failure.errors.some((error) => error.stage === "admin.context_close"));
  assert.equal(failure.errors.filter((error) => error.stage === "browser.route.lifecycle_unit").length, 1);
  const safeDiagnostics = errorDiagnosticSurface(failure.errors);
  assert.equal(safeDiagnostics.includes(TEST_SECRETS.uiToken), false);
  assert.equal(safeDiagnostics.includes(TEST_SECRETS.providerKey), false);
  assert.ok(order.indexOf("primary_abort") < order.indexOf("logout"));
  assert.ok(order.indexOf("sealed_abort_end") > order.indexOf("close"));
});

test("browser close lifecycle preserves the role failure when browser close also fails", async () => {
  let closeCount = 0;
  let failure;
  try {
    await runBrowserCloseLifecycle({
      /** 主阶段使用已脱敏 Route 错误，模拟角色生命周期失败。 */
      async executeAction() {
        throw new BrowserAcceptanceStageError("browser.route.outer_unit", "route_handler_failed");
      },
      /** Browser close 故意抛含 Secret 原错，公开聚合只能保留固定 stage/code。 */
      async closeAction() {
        closeCount += 1;
        throw new Error(TEST_SECRETS.providerKey);
      },
    });
  } catch (error) {
    failure = error;
  }
  assert.equal(closeCount, 1);
  assert.ok(failure instanceof AggregateError);
  assert.deepEqual(
    failure.errors.map((error) => [error.stage, error.code]),
    [
      ["browser.route.outer_unit", "route_handler_failed"],
      ["browser.close", "browser_close_failed"],
    ],
  );
  assert.equal(errorDiagnosticSurface(failure.errors).includes(TEST_SECRETS.providerKey), false);
});

test("409 refresh evidence requires a later fully scanned GET with the exact version", () => {
  // 响应工厂默认补齐 Header、JSON 和隐私扫描证据，单测只覆盖时序与版本差异。
  const response = (overrides) => ({
    responseIndex: 0,
    requestIdPresent: true,
    traceIdPresent: true,
    noStore: true,
    bodyPrivacyScanned: true,
    bodyJsonValid: true,
    cacheAffinityVersion: null,
    ...overrides,
  });
  const records = [
    response({ responseIndex: 0, method: "GET", surface: "model_alias_list", status: 200, cacheAffinityVersion: 3 }),
    response({ responseIndex: 1, method: "PATCH", surface: "cache_config_write", status: 409 }),
    response({ responseIndex: 2, method: "GET", surface: "model_alias_list", status: 200, cacheAffinityVersion: 4 }),
  ];
  assert.deepEqual(assertConflictRefreshEvidence(records, 4), {
    conflictIndex: 1,
    refreshResponseIndex: 2,
    version: 4,
  });
  assert.throws(() => assertConflictRefreshEvidence(records.slice(0, 2), 4));
  assert.throws(() => assertConflictRefreshEvidence([
    records[0], records[1], { ...records[2], bodyPrivacyScanned: false },
  ], 4));
});

test("transport console window scopes expected 409 or abort errors and closes its allowance", () => {
  const evidence = browserGuardEvidenceFixture();
  const window = beginExpectedTransportConsoleError(evidence, "cache_config_write");
  assert.deepEqual(recordBrowserConsoleError(evidence, {
    text: "Failed to load resource: the server responded with a status of 409 (Conflict)",
    locationUrl: `${ORIGINS.controlOrigin}/model-aliases/alias_unit/cache-affinity`,
  }, ORIGINS), { expected: true });
  // 同一窗口即使仍活动也只能接纳一条，避免重复消息变成无限豁免配额。
  assert.deepEqual(recordBrowserConsoleError(evidence, {
    text: "Failed to load resource: the server responded with a status of 409 (Conflict)",
    locationUrl: `${ORIGINS.controlOrigin}/model-aliases/alias_unit/cache-affinity`,
  }, ORIGINS), { expected: false });
  assert.deepEqual(settleExpectedTransportConsoleError(evidence, window), { observed: 1, settled: true });
  assert.deepEqual(assertTransportConsoleConverged(evidence), { expected: 1, observed: 1, settled: 1 });
  assert.deepEqual(recordBrowserConsoleError(evidence, {
    text: "Failed to load resource: the server responded with a status of 409 (Conflict)",
    locationUrl: `${ORIGINS.controlOrigin}/model-aliases/alias_unit/cache-affinity`,
  }, ORIGINS), { expected: false });
  assert.deepEqual(evidence.browserErrors, ["console_transport_error", "console_transport_error"]);

  const wrongSurfaceEvidence = browserGuardEvidenceFixture();
  const wrongSurfaceWindow = beginExpectedTransportConsoleError(wrongSurfaceEvidence, "cache_config_write");
  assert.deepEqual(recordBrowserConsoleError(wrongSurfaceEvidence, {
    text: "Failed to load resource: the server responded with a status of 409 (Conflict)",
    locationUrl: `${ORIGINS.controlOrigin}/model-aliases`,
  }, ORIGINS), { expected: false });
  assert.deepEqual(settleExpectedTransportConsoleError(wrongSurfaceEvidence, wrongSurfaceWindow), {
    observed: 0,
    settled: true,
  });
  assert.deepEqual(assertTransportConsoleConverged(wrongSurfaceEvidence), {
    expected: 1,
    observed: 0,
    settled: 1,
  });
  assert.deepEqual(wrongSurfaceEvidence.browserErrors, ["console_transport_error"]);

  // 浏览器对同一 409 可能不输出 Console 消息；干净窗口 observed=0 也应独立收敛通过。
  const silentEvidence = browserGuardEvidenceFixture();
  const silentWindow = beginExpectedTransportConsoleError(silentEvidence, "cache_config_write");
  assert.deepEqual(settleExpectedTransportConsoleError(silentEvidence, silentWindow), {
    observed: 0,
    settled: true,
  });
  assert.deepEqual(assertTransportConsoleConverged(silentEvidence), { expected: 1, observed: 0, settled: 1 });
  assert.deepEqual(silentEvidence.browserErrors, []);

  const wrongTextEvidence = browserGuardEvidenceFixture();
  const wrongTextWindow = beginExpectedTransportConsoleError(wrongTextEvidence, "cache_config_write");
  assert.deepEqual(recordBrowserConsoleError(wrongTextEvidence, {
    text: "Uncaught TypeError: synthetic conflict handler failure",
    locationUrl: `${ORIGINS.controlOrigin}/model-aliases/alias_unit/cache-affinity`,
  }, ORIGINS), { expected: false });
  assert.deepEqual(settleExpectedTransportConsoleError(wrongTextEvidence, wrongTextWindow), {
    observed: 0,
    settled: true,
  });
  assert.deepEqual(wrongTextEvidence.browserErrors, ["console_type_error"]);

  const corsCombinationEvidence = browserGuardEvidenceFixture();
  const corsCombinationWindow = beginExpectedTransportConsoleError(
    corsCombinationEvidence,
    "cache_config_write",
  );
  assert.deepEqual(recordBrowserConsoleError(corsCombinationEvidence, {
    text: "Failed to load resource: net::ERR_FAILED; Access to fetch was blocked by CORS policy",
    locationUrl: `${ORIGINS.controlOrigin}/model-aliases/alias_unit/cache-affinity`,
  }, ORIGINS), { expected: false });
  assert.deepEqual(
    settleExpectedTransportConsoleError(corsCombinationEvidence, corsCombinationWindow),
    { observed: 0, settled: true },
  );
  assert.deepEqual(corsCombinationEvidence.browserErrors, ["console_cors_error"]);

  const acceptanceSource = readFileSync(
    new URL("../../../scripts/acceptance/prd050-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  const conflictStart = acceptanceSource.indexOf('const conflictMutation = registerMutation(state, "ui_conflict")');
  const windowStart = acceptanceSource.indexOf(
    'beginExpectedTransportConsoleError(evidence, "cache_config_write")', conflictStart,
  );
  const responseScan = acceptanceSource.indexOf(
    "const conflictBody = await scanTargetResponse(conflictResponse, state, evidence)", conflictStart,
  );
  const refreshEvidence = acceptanceSource.indexOf(
    "conflictRefresh = assertConflictRefreshEvidence(", conflictStart,
  );
  const finallyStart = acceptanceSource.indexOf("} finally {", refreshEvidence);
  const windowSettle = acceptanceSource.indexOf(
    "settleExpectedTransportConsoleError(evidence, conflictTransportWindow)", conflictStart,
  );
  const nextPatchFlow = acceptanceSource.indexOf("const errorValue =", conflictStart);
  assert.ok(
    conflictStart >= 0
      && windowStart > conflictStart
      && responseScan > windowStart
      && refreshEvidence > responseScan
      && finallyStart > refreshEvidence
      && windowSettle > finallyStart
      && nextPatchFlow > windowSettle,
  );
  assert.match(
    acceptanceSource.slice(conflictStart, nextPatchFlow),
    /try \{[\s\S]*conflictRefresh = assertConflictRefreshEvidence\([\s\S]*\} finally \{\s*settleExpectedTransportConsoleError\(evidence, conflictTransportWindow\);\s*\}/,
  );
});

test("unexpected console errors collapse to fixed categories without retaining their text", () => {
  const cases = [
    ["Failed to load resource: net::ERR_FAILED", "console_transport_error"],
    ["Access to fetch at local endpoint was blocked by CORS policy", "console_cors_error"],
    ["Failed to load resource: net::ERR_FAILED after a cross-origin CORS failure", "console_cors_error"],
    ["An invalid form control is not focusable", "console_form_error"],
    ["Uncaught TypeError: synthetic unit failure", "console_type_error"],
    ["Uncaught ReferenceError: synthetic", "console_reference_error"],
    ["Uncaught SyntaxError: synthetic", "console_syntax_error"],
    ["Uncaught RangeError: synthetic", "console_range_error"],
    ["Uncaught synthetic browser failure", "console_uncaught_error"],
    ["Error: synthetic browser failure", "console_error_object"],
    ["Refused to connect because of Content Security Policy", "console_policy_error"],
    ["synthetic browser diagnostic", "console_other_error"],
  ];
  for (const [message, expected] of cases) {
    const category = classifyUnexpectedBrowserConsoleError(message);
    assert.equal(category, expected);
    assert.equal(category.includes(message), false);
  }
});

test("retry cancel contract requires one request id and restores committed authority", () => {
  const safe = {
    abortedRequestId: "req_prd050_browser_retry_unit",
    retryRequestId: "req_prd050_browser_retry_unit",
    committedValue: 1024,
    cancelledDraftValue: 1030,
    restoredValue: 1024,
    saveDisabled: true,
  };
  assert.equal(assertRetryCancelEvidence(safe).cancelRestoredAuthority, true);
  assert.throws(() => assertRetryCancelEvidence({ ...safe, retryRequestId: "req_other" }));
  assert.throws(() => assertRetryCancelEvidence({ ...safe, restoredValue: 1030 }));
});

test("session switch contract rejects reused sessions stale callbacks and wrong convergence", () => {
  const safe = {
    priorSessionId: "session_prd050_old",
    nextSessionId: "session_prd050_new",
    logoutCompleted: true,
    authStorageCleared: true,
    lateResponseIgnored: true,
    committedValue: 1031,
    convergedValue: 1031,
  };
  assert.equal(assertSessionSwitchEvidence(safe).sessionChanged, true);
  assert.throws(() => assertSessionSwitchEvidence({ ...safe, nextSessionId: safe.priorSessionId }));
  assert.throws(() => assertSessionSwitchEvidence({ ...safe, authStorageCleared: false }));
  assert.throws(() => assertSessionSwitchEvidence({ ...safe, convergedValue: 1032 }));
});

test("PAGE-072 loading contract rejects login form and premature configuration DOM", () => {
  const safe = {
    activeNavigationId: "routing-policies",
    activeNavigationButtonCount: 1,
    panelCount: 1,
    titleText: "路由策略",
    loadingText: "等待 /model-aliases 响应。",
    loginFormCount: 0,
    configurationRowCount: 0,
    editorFormCount: 0,
  };
  assert.deepEqual(assertPage072LoadingEvidence(safe), {
    activeNavigationId: "routing-policies",
    panelPresent: true,
    loginFormAbsent: true,
    configurationAbsent: true,
  });
  for (const invalid of [
    { activeNavigationId: "overview" },
    { activeNavigationButtonCount: 0 },
    { panelCount: 0 },
    { titleText: "登录" },
    { loadingText: "登录中" },
    { loginFormCount: 1 },
    { configurationRowCount: 1 },
    { editorFormCount: 1 },
  ]) {
    assert.throws(() => assertPage072LoadingEvidence({ ...safe, ...invalid }));
  }
});

test("viewport state matrix requires loading empty five PAGE-072 states and three PAGE-073 states", () => {
  assert.deepEqual(REQUIRED_VIEWPORT_STATES, [
    "loading", "empty", "configured", "editing", "saving", "conflict", "error",
    "page073_pending", "page073_warm", "page073_rebound",
  ]);
  const matrix = Object.fromEntries(REQUIRED_VIEWPORT_STATES.map((stateKey) => [stateKey, {
    layouts: Object.fromEntries(VIEWPORTS.map((viewport) => [viewport.name, { horizontalOverflow: false }])),
    screenshots: VIEWPORTS.map((viewport) => ({
      viewportName: viewport.name,
      sha256: "a".repeat(64),
      bytes: 1024,
      nonBlank: true,
      privacyMaskApplied: true,
    })),
  }]));
  assert.deepEqual(assertViewportStateMatrix(matrix), { stateCount: 10, viewportCount: 3 });
  const missingLoading = { ...matrix };
  delete missingLoading.loading;
  assert.throws(() => assertViewportStateMatrix(missingLoading));
  assert.throws(() => assertViewportStateMatrix({
    ...matrix,
    error: { ...matrix.error, screenshots: matrix.error.screenshots.slice(0, 2) },
  }));
  assert.throws(() => assertViewportStateMatrix({
    ...matrix,
    error: {
      ...matrix.error,
      screenshots: matrix.error.screenshots.map((item, index) => (
        index === 0 ? { ...item, sha256: "invalid", bytes: 0 } : item
      )),
    },
  }));
});

test("best effort cleanup runs every stage and aggregates only safe stage errors", async () => {
  const calls = [];
  let failure;
  try {
    await runBestEffortCleanupStages([
      ["config", async () => { calls.push("config"); throw new Error(TEST_SECRETS.providerKey); }],
      ["api_logout", async () => { calls.push("api_logout"); return { logoutCount: 3 }; }],
      ["database", async () => { calls.push("database"); return { verified: true }; }],
      ["ledger", async () => { calls.push("ledger"); return { mutationCount: 9 }; }],
    ]);
  } catch (error) {
    failure = error;
  }
  assert.deepEqual(calls, ["config", "api_logout", "database", "ledger"]);
  assert.ok(failure instanceof AggregateError);
  assert.equal(failure.errors[0].stage, "browser.cleanup.config");
  assert.equal(failure.errors[0].message.includes(TEST_SECRETS.providerKey), false);
  const passed = await runBestEffortCleanupStages([
    ["config", async () => ({ restored: true })],
    ["database", async () => ({ verified: true })],
  ]);
  assert.deepEqual(passed, { config: { restored: true }, database: { verified: true } });
});

test("browser evidence directory rejects reentry before changing old result or PNG bytes", async () => {
  const temporaryRoot = mkdtempSync(path.join(tmpdir(), "metahub-prd050-browser-dir-"));
  try {
    const browserDirectory = path.join(temporaryRoot, "browser");
    assert.equal(await createBrowserEvidenceDirectory(browserDirectory), browserDirectory);
    const resultPath = path.join(browserDirectory, "browser-result.json");
    const pngPath = path.join(browserDirectory, "admin-page-072-desktop.png");
    writeFileSync(resultPath, "old-result", { flag: "wx" });
    writeFileSync(pngPath, "old-png", { flag: "wx" });

    await assert.rejects(
      () => createBrowserEvidenceDirectory(browserDirectory),
      (error) => error?.code === "EEXIST",
    );
    assert.equal(readFileSync(resultPath, "utf8"), "old-result");
    assert.equal(readFileSync(pngPath, "utf8"), "old-png");
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("PNG evidence uses exclusive creation and preserves a conflicting file", async () => {
  const temporaryRoot = mkdtempSync(path.join(tmpdir(), "metahub-prd050-browser-png-"));
  try {
    const pngPath = path.join(temporaryRoot, "admin-page-073-warm-mobile.png");
    assert.equal(await writeNewEvidenceFile(pngPath, Buffer.from("first-png")), pngPath);
    await assert.rejects(
      () => writeNewEvidenceFile(pngPath, Buffer.from("replacement-png")),
      (error) => error?.code === "EEXIST",
    );
    assert.equal(readFileSync(pngPath, "utf8"), "first-png");
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("result publication is write-once and removes only its owned temporary files", async () => {
  const temporaryRoot = mkdtempSync(path.join(tmpdir(), "metahub-prd050-browser-result-"));
  try {
    const resultPath = path.join(temporaryRoot, "browser-result.json");
    const createdTemporaryPaths = [];
    // 可预测工厂仍生成当前 PID 所有的同目录名称，便于逐次核对成功和冲突分支的清理结果。
    const temporaryPathFactory = () => {
      const sequence = String(createdTemporaryPaths.length + 1).padStart(36, "0");
      const temporaryPath = path.join(
        temporaryRoot,
        `.browser-result.json.${process.pid}.${sequence}.tmp`,
      );
      createdTemporaryPaths.push(temporaryPath);
      return temporaryPath;
    };

    assert.equal(
      await publishWriteOnceResult(resultPath, "first-result", { temporaryPathFactory }),
      path.resolve(resultPath),
    );
    assert.equal(readFileSync(resultPath, "utf8"), "first-result");
    assert.equal(existsSync(createdTemporaryPaths[0]), false);

    await assert.rejects(
      () => publishWriteOnceResult(resultPath, "replacement-result", { temporaryPathFactory }),
      (error) => error?.code === "EEXIST",
    );
    assert.equal(readFileSync(resultPath, "utf8"), "first-result");
    assert.equal(existsSync(createdTemporaryPaths[1]), false);
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("result publication never deletes a colliding temporary file owned by another process", async () => {
  const temporaryRoot = mkdtempSync(path.join(tmpdir(), "metahub-prd050-browser-foreign-"));
  try {
    const resultPath = path.join(temporaryRoot, "browser-result.json");
    const foreignTemporaryPath = path.join(
      temporaryRoot,
      `.browser-result.json.${process.pid}.${"f".repeat(36)}.tmp`,
    );
    writeFileSync(foreignTemporaryPath, "foreign-owner", { flag: "wx" });
    // 工厂故意返回已存在路径，证明 open("wx") 失败前不会取得清理所有权。
    const temporaryPathFactory = () => foreignTemporaryPath;

    await assert.rejects(
      () => publishWriteOnceResult(resultPath, "new-result", { temporaryPathFactory }),
      (error) => error?.code === "EEXIST",
    );
    assert.equal(readFileSync(foreignTemporaryPath, "utf8"), "foreign-owner");
    assert.equal(existsSync(resultPath), false);
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("result temporary paths stay random unique and in the destination directory", () => {
  const resultPath = path.resolve(tmpdir(), "browser-result.json");
  const first = createResultTemporaryPath(resultPath);
  const second = createResultTemporaryPath(resultPath);
  assert.equal(path.dirname(first), path.dirname(resultPath));
  assert.equal(path.dirname(second), path.dirname(resultPath));
  assert.notEqual(first, second);
  assert.match(
    path.basename(first),
    new RegExp(`^\\.browser-result\\.json\\.${process.pid}\\.[0-9a-f]{36}\\.tmp$`),
  );
});
