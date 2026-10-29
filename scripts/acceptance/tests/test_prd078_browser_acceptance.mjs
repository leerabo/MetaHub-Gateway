/** PRD-078 Browser runner 的路径、网络、隐私与 exact 完成信号合同。 */

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  ALLOWED_ORIGINS,
  assertLoginAttemptComplete,
  attachHealthRequestHeaders,
  beginLoginAttempt,
  BrowserAcceptanceFailure,
  CONTROL_ORIGIN,
  DEBUG_PORT,
  DRIVER_ROOT,
  HARNESS_CONSOLE_URL,
  HARNESS_ORIGIN,
  SCREENSHOTS,
  VIEWPORTS,
  assertCancellationLedger,
  assertStepUpConsoleLedger,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyBrowserConsoleError,
  classifyConsoleUrl,
  classifyHealthPreflight,
  classifyHealthRequest,
  classifyUnexpectedConsoleReason,
  classifyRequest,
  classifyStepUpPreflight,
  closeBrowser,
  createPrivateConfiguration,
  directChild,
  harnessEnvironment,
  installBackgroundRefreshClockGate,
  parseArguments,
  parseHealthAttemptRequestId,
  recordBrowserConsoleEntry,
  recordNetworkLoadingFailure,
  recordNetworkLoadingFinished,
  recordNetworkRequest,
  recordNetworkResponse,
  registerHealthPreflightRequest,
  registerFetchStepUpOwner,
  registerPausedStepUpResponse,
  registerStepUpPreflightRequest,
  registerStepUpPreflightResponse,
  restoreBackgroundRefreshClockGate,
  safeRunId,
  terminateWindowsProcessTree,
  validateCompletionFacts,
  validateBackgroundRefreshClockGateFacts,
  waitForLoginCompletion,
  waitForRequestOwnersDrain,
} from "../prd078-browser-acceptance.mjs";


// 测试路径只从当前合同文件推导仓库根，避免使用宿主目录或外部临时位置。
const TEST_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(TEST_DIRECTORY, "..", "..", "..");


/** 构造不含真实像素或业务正文的截图合同事实，用于验证完成信号形状。 */
function screenshotFacts(width, height) {
  return Object.freeze({
    pixels: Object.freeze({ non_blank_pixels: true }),
    layout: Object.freeze({
      width,
      height,
      horizontal_overflow: false,
      text_overflow: false,
      overlap_count: 0,
    }),
  });
}


/** 构造全部布尔门禁均满足的脱敏完成事实，不包含凭据、审批 ID 或响应正文。 */
function completeFacts() {
  return buildCompletionFacts({
    screenshot: screenshotFacts(1280, 720),
    financeOnly: true,
    nativeModal: true,
    backgroundBlocked: true,
    standardInitialFocus: true,
    tabForwardWrap: true,
    tabReverseWrap: true,
    idleEscapeClosed: true,
    focusRestored: true,
  }, {
    idleScreenshot: screenshotFacts(390, 844),
    busyScreenshot: screenshotFacts(390, 844),
    securityOnly: true,
    nativeModal: true,
    highRiskInitialFocus: true,
    idleEscapeClosed: true,
    idleEscapePasswordCleared: true,
    busyNativeModal: true,
    busyEscapeBlocked: true,
    busyCancelBlocked: true,
    stepUpCompleted: true,
    ownerDriftClosed: true,
    ownerDriftPasswordCleared: true,
    ownerDriftFocusNotRestored: true,
    lateSuccessDiscarded: true,
    permissionRevocationClosed: true,
    permissionRevocationPasswordCleared: true,
    permissionRevocationRootCleared: true,
    lateDenialDiscarded: true,
    logoutDialogClosed: true,
    logoutPasswordCleared: true,
    logoutRootCleared: true,
    logoutStorageCleared: true,
  }, {
    externalRequests: 0,
    unexpectedConsoleErrors: 0,
    runtimeExceptions: 0,
  });
}


/** 创建最小网络账本夹具，所有 owner 与计数仅供单个合同用例使用。 */
function cancellationTracker(owners = []) {
  return {
    privateValues: [],
    processPrivacyViolations: 0,
    phase: "late_success",
    networkActivityRevision: 0,
    requestOwners: new Map(owners),
    fetchStepUpOwners: new Map(),
    stepUpPreflightOwners: new Map(),
    healthPreflightOwners: new Map(),
    seenResponses: new Set(),
    expectedCanceledRequestIds: new Set(),
    canceledStepUpRequestCount: 0,
    healthProbeFailureCount: 0,
    healthProbeRecoveryCount: 0,
    healthRequestLedgers: new Map(),
    healthRequestIds: new Map(),
    healthRequestHeaderHints: new Map(),
    loginAttempts: [],
    loginRequestProofs: new Map(),
    activeLoginAttempt: null,
    loginAttemptSequence: 0,
    unexpectedNetworkFailureCount: 0,
    unexpectedNetworkFailure: null,
    consoleNetworkFailureCount: 0,
    expectedStepUpConsoleErrors: 0,
    unexpectedConsoleErrors: 0,
    unexpectedConsoleReasons: new Map(),
    loginStatuses: [],
    logoutStatuses: [],
    listStatuses: [],
    detailStatuses: [],
    stepUpStatuses: [],
    decisionStatuses: [],
  };
}


/** 构造只含固定 auth 路由和方法的登录 Network 事件，不包含凭据或响应正文。 */
function loginNetworkEvent(requestId, category, overrides = {}) {
  return {
    requestId,
    request: {
      url: `${CONTROL_ORIGIN}${category === "auth_login" ? "/auth/login" : "/auth/me"}`,
      method: category === "auth_login" ? "POST" : "GET",
      headers: {},
      ...overrides,
    },
  };
}


/** 建立一次 owner，并登记两段请求，供 response/terminal 反序合同复用。 */
function registerLoginChain(tracker, role = "finance") {
  tracker.phase = `${role}_login`;
  const attempt = beginLoginAttempt(tracker, role, `browser.login.${role}`);
  recordNetworkRequest(tracker, loginNetworkEvent("login-request", "auth_login"));
  return attempt;
}


function stepUpFetchEvent(requestId, networkId, {
  method = "POST",
  pathname = "/approvals/apr_contract/step-up",
} = {}) {
  return {
    requestId,
    networkId,
    request: {
      url: `${CONTROL_ORIGIN}${pathname}`,
      method,
      headers: {},
    },
  };
}


/** 构造与 Fetch 事件共享 networkId 的 step-up Network 请求，不包含真实任务标识。 */
function stepUpNetworkEvent(networkId, options = {}) {
  const event = stepUpFetchEvent("fetch-request", networkId, options);
  return { requestId: networkId, request: event.request };
}


/** 构造固定回环 Origin 和允许头的 step-up CORS 预检事件。 */
function stepUpPreflightEvent(requestId, networkId, overrides = {}) {
  return {
    requestId,
    networkId,
    request: {
      url: `${CONTROL_ORIGIN}/approvals/apr_contract/step-up`,
      method: "OPTIONS",
      headers: {
        Origin: "http://127.0.0.1:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,authorization",
      },
      ...overrides,
    },
  };
}


/** 构造带 logical attempt request-id 的健康 GET，用于验证两次尝试账本。 */
function healthRequestEvent(
  requestId,
  logicalId,
  attempt,
  category = "gateway_health_live",
) {
  const [service, route] = category.split("_health_");
  const port = service === "gateway" ? 8000 : 8001;
  const pathname = route === "summary" ? "/health" : route === "version" ? "/version" : `/health/${route}`;
  return {
    requestId,
    request: {
      url: `http://localhost:${port}${pathname}`,
      method: "GET",
      headers: { "X-Request-ID": `${logicalId}_attempt_${attempt}` },
    },
  };
}


/** 构造健康 GET 对应的固定 CORS OPTIONS 事件，支持少量非法形状覆盖。 */
function healthPreflightEvent(requestId, networkId, {
  port = 8001,
  pathname = "/health/live",
  method = "OPTIONS",
  headers = {
    Origin: "http://127.0.0.1:5173",
    "Access-Control-Request-Method": "GET",
    "Access-Control-Request-Headers": "x-request-id",
  },
} = {}) {
  return {
    requestId,
    networkId,
    request: {
      url: `http://localhost:${port}${pathname}`,
      method,
      headers,
    },
  };
}


/** 构造允许 GET 与 X-Request-ID 的健康 CORS 响应头，不含认证或业务数据。 */
function healthPreflightResponseHeaders(origin = "http://127.0.0.1:5173") {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Request-ID, X-Trace-ID",
  };
}


test("runner freezes repository paths, loopback ports and three screenshot names", () => {
  assert.equal(DRIVER_ROOT, path.join(ROOT, ".tmp", "prd078-browser-driver"));
  assert.equal(CONTROL_ORIGIN, "http://localhost:8001");
  assert.equal(HARNESS_ORIGIN, "http://127.0.0.1:18881");
  assert.equal(HARNESS_CONSOLE_URL, "http://127.0.0.1:5173/#approvals");
  assert.equal(DEBUG_PORT, 19078);
  assert.deepEqual(VIEWPORTS, {
    desktop: { name: "desktop-1280x720", width: 1280, height: 720 },
    mobile: { name: "mobile-390x844", width: 390, height: 844 },
  });
  assert.deepEqual(SCREENSHOTS, [
    "finance-standard-modal-desktop-1280x720.png",
    "security-high-risk-modal-mobile-390x844.png",
    "security-high-risk-busy-mobile-390x844.png",
  ]);
  assert.ok([...ALLOWED_ORIGINS].every((origin) => new URL(origin).hostname === "localhost"
    || new URL(origin).hostname === "127.0.0.1"));
});


test("run id and direct-child mapping reject traversal and extra arguments", () => {
  assert.equal(safeRunId("prd078-unit-01"), "prd078-unit-01");
  assert.deepEqual(parseArguments(["--run-id", "prd078-unit-01"]), { runId: "prd078-unit-01" });
  for (const value of ["..", "../outside", "C:/outside", "a/b", "a\\b", "contains space"]) {
    assert.throws(() => safeRunId(value), BrowserAcceptanceFailure);
  }
  assert.throws(() => parseArguments(["--run-id", "valid", "--extra", "x"]), BrowserAcceptanceFailure);
  assert.equal(directChild(DRIVER_ROOT, "prd078-unit-01", "test.path"), path.join(DRIVER_ROOT, "prd078-unit-01"));
  assert.throws(() => directChild(DRIVER_ROOT, "../outside", "test.path"), BrowserAcceptanceFailure);
});


test("private configuration is unique and harness environment strips host credentials", () => {
  const first = createPrivateConfiguration();
  const second = createPrivateConfiguration();
  assert.notEqual(first.rolePassword, second.rolePassword);
  assert.notEqual(first.stopToken, second.stopToken);
  assert.ok(first.rolePassword.length >= 32 && first.stopToken.length >= 43);
  const environment = harnessEnvironment(first, path.join(ROOT, ".tmp", "driver"), {
    PATH: "repository-tools",
    SYSTEMROOT: "C:\\Windows",
    GITHUB_TOKEN: "private-github",
    OPENAI_API_KEY: "private-openai",
    HTTP_PROXY: "http://outside.invalid",
    USERPROFILE: "C:\\Users\\outside",
  });
  assert.equal(environment.PATH, "repository-tools");
  assert.equal(environment.PRD078_BROWSER_ROLE_PASSWORD, first.rolePassword);
  assert.equal(environment.PRD078_BROWSER_STOP_TOKEN, first.stopToken);
  for (const name of ["GITHUB_TOKEN", "OPENAI_API_KEY", "HTTP_PROXY", "USERPROFILE"]) {
    assert.equal(environment[name], undefined);
  }
});


test("browser clock gate freezes only the 15 second shell refresh and restores native timers", () => {
  const nativeCalls = [];
  /** 记录非目标计时器委托，证明验收 gate 不会吞掉审批倒计时。 */
  const nativeSetInterval = function nativeSetInterval(callback, delay, ...args) {
    nativeCalls.push({ receiver: this, callback, delay, args });
    return 78;
  };
  const target = { setInterval: nativeSetInterval };
  assert.equal(installBackgroundRefreshClockGate(target), true);
  assert.equal(target.setInterval(() => {}, 15_000), 0);
  assert.equal(nativeCalls.length, 0);
  assert.equal(target.setInterval(() => {}, "15000"), 78);
  assert.equal(target.setInterval(() => {}, 30_000, "countdown"), 78);
  assert.equal(nativeCalls.length, 2);
  assert.equal(nativeCalls[1].receiver, target);
  assert.equal(nativeCalls[1].delay, 30_000);
  assert.deepEqual(nativeCalls[1].args, ["countdown"]);

  const facts = restoreBackgroundRefreshClockGate(target);
  assert.deepEqual(facts, { capturedCount: 1, restored: true });
  assert.equal(validateBackgroundRefreshClockGateFacts(facts), true);
  assert.equal(target.setInterval, nativeSetInterval);
  assert.equal(Object.hasOwn(target, "__prd078BackgroundRefreshGate"), false);
  assert.equal(restoreBackgroundRefreshClockGate(target).restored, false);
  assert.throws(
    () => validateBackgroundRefreshClockGateFacts({ capturedCount: 0, restored: true }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "background_refresh_count",
  );
  assert.throws(
    () => validateBackgroundRefreshClockGateFacts({ capturedCount: 2, restored: true }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "background_refresh_count",
  );
  assert.throws(
    () => validateBackgroundRefreshClockGateFacts({ capturedCount: 1, restored: false }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "restore",
  );
});


test("request guard allows only loopback origins and rejects credential-like query keys", () => {
  assert.equal(browserRequestAllowed("http://localhost:5173/main.js"), true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/approvals"), true);
  assert.equal(browserRequestAllowed("data:text/plain,ok"), true);
  assert.equal(browserRequestAllowed("https://example.com/"), false);
  assert.equal(browserRequestAllowed("http://localhost:8001/approvals?token=private"), false);
  assert.equal(browserRequestAllowed("http://user:pass@localhost:8001/health"), false);
  assert.equal(classifyRequest("http://localhost:8001/approvals", "GET"), "approval_list");
  assert.equal(classifyRequest("http://localhost:8001/approvals/apr_private/step-up", "POST"), "approval_step_up");
  assert.equal(classifyRequest("http://localhost:8001/approvals/apr_private/approve", "POST"), "approval_decision");
  for (const [route, suffix] of [
    ["/health", "health_summary"],
    ["/health/live", "health_live"],
    ["/health/ready", "health_ready"],
    ["/version", "health_version"],
  ]) {
    assert.equal(classifyRequest(`http://localhost:8000${route}`, "GET"), `gateway_${suffix}`);
    assert.equal(classifyRequest(`http://localhost:8001${route}`, "GET"), `control_${suffix}`);
    for (const method of ["POST", "PUT", "HEAD", "DELETE"]) {
      assert.equal(classifyRequest(`http://localhost:8000${route}`, method), "gateway_other");
      assert.equal(classifyRequest(`http://localhost:8001${route}`, method), "control_other");
    }
  }
  assert.equal(classifyHealthRequest("http://localhost:8000/healthz", "GET"), null);
  assert.equal(classifyHealthRequest("http://localhost:8000/health", "POST"), null);
  assert.equal(classifyHealthRequest("http://localhost:8000/health?probe=1", "GET"), null);
  assert.equal(classifyRequest("http://localhost:8000/health", "OPTIONS"), "gateway_health_summary");
  assert.equal(classifyRequest("http://localhost:8001/health/live", "OPTIONS"), "control_health_live");
  assert.equal(classifyRequest("http://localhost:8000/v1/models", "GET"), "gateway_other");
  assert.equal(classifyRequest("http://localhost:5173/main.js", "GET"), "console_asset");
  assert.equal(classifyRequest("http://localhost:8001/auth/me", "GET"), "auth_me");
  assert.equal(classifyRequest("http://localhost:8001/console/navigation", "GET"), "console_navigation");
  assert.equal(classifyRequest("http://localhost:8001/dashboard/role-view", "GET"), "dashboard_role_view");
  assert.equal(classifyRequest("http://localhost:8001/budgets/summary", "GET"), "control_budgets");
  assert.equal(classifyRequest("http://localhost:8001/unknown/private-id", "GET"), "control_other");
  assert.equal(classifyRequest("https://example.com/", "GET"), "external");
});


test("login ledger binds POST /auth/login then first GET /auth/me across CDP event order", () => {
  for (const order of ["response_first", "terminal_first"]) {
    const tracker = cancellationTracker();
    const attempt = registerLoginChain(tracker);
    /** 按两种 CDP 反序收敛同一登录 leg，验证 response 与 terminal 可交换。 */
    const settleLeg = (requestId) => {
      if (order === "response_first") {
        assert.equal(recordNetworkResponse(tracker, {
          requestId,
          response: { status: 200 },
        }), "login_response_waiting_terminal");
        assert.equal(
          recordNetworkLoadingFinished(tracker, { requestId }),
          "login_terminal_settled",
        );
      } else {
        assert.equal(
          recordNetworkLoadingFinished(tracker, { requestId }),
          "login_terminal_waiting_response",
        );
        assert.equal(recordNetworkResponse(tracker, {
          requestId,
          response: { status: 200 },
        }), "login_response_settled");
      }
    };
    settleLeg("login-request");
    assert.equal(attempt.state, "await_me");
    recordNetworkRequest(tracker, loginNetworkEvent("me-request", "auth_me"));
    settleLeg("me-request");
    assert.equal(assertLoginAttemptComplete(attempt, "browser.login.finance"), true);
    assert.equal(attempt.state, "completed");
    assert.equal(tracker.requestOwners.size, 0);
    assert.deepEqual(tracker.loginStatuses, [200]);
    assert.equal(tracker.unexpectedNetworkFailureCount, 0);
  }
});


test("login ledger keeps auth CORS preflights outside the business proof", () => {
  const tracker = cancellationTracker();
  tracker.phase = "finance_login";
  const attempt = beginLoginAttempt(tracker, "finance", "browser.login.finance");
  recordNetworkRequest(tracker, loginNetworkEvent("login-options", "auth_login", {
    method: "OPTIONS",
  }));
  recordNetworkResponse(tracker, { requestId: "login-options", response: { status: 200 } });
  recordNetworkLoadingFinished(tracker, { requestId: "login-options" });
  assert.equal(attempt.state, "await_login");
  assert.equal(attempt.login.requestId, null);
  assert.equal(tracker.loginRequestProofs.size, 0);
  assert.deepEqual(tracker.loginStatuses, []);
  assert.equal(tracker.unexpectedNetworkFailureCount, 0);
});


test("login ledger fails closed on sequence, duplicate, phase, route and request-id drift", () => {
  const earlyMe = cancellationTracker();
  earlyMe.phase = "finance_login";
  beginLoginAttempt(earlyMe, "finance", "browser.login.finance");
  recordNetworkRequest(earlyMe, loginNetworkEvent("early-me", "auth_me"));
  assert.equal(earlyMe.unexpectedNetworkFailure?.reason, "login_sequence");

  const duplicate = cancellationTracker();
  registerLoginChain(duplicate);
  recordNetworkRequest(duplicate, loginNetworkEvent("second-login", "auth_login"));
  assert.equal(duplicate.unexpectedNetworkFailure?.reason, "login_duplicate_request");

  const wrongPhase = cancellationTracker();
  registerLoginChain(wrongPhase);
  wrongPhase.phase = "security_login";
  recordNetworkResponse(wrongPhase, { requestId: "login-request", response: { status: 200 } });
  assert.equal(wrongPhase.unexpectedNetworkFailure?.reason, "login_phase");

  const wrongRoute = cancellationTracker();
  wrongRoute.phase = "finance_login";
  beginLoginAttempt(wrongRoute, "finance", "browser.login.finance");
  recordNetworkRequest(wrongRoute, loginNetworkEvent("wrong-route", "auth_login", {
    url: `${CONTROL_ORIGIN}/auth/login?debug=1`,
  }));
  assert.equal(wrongRoute.unexpectedNetworkFailure?.reason, "login_route");

  const reused = cancellationTracker();
  reused.requestOwners.set("reused", {
    category: "console_navigation",
    method: "GET",
    phase: "finance_login",
  });
  reused.phase = "finance_login";
  beginLoginAttempt(reused, "finance", "browser.login.finance");
  recordNetworkRequest(reused, loginNetworkEvent("reused", "auth_login"));
  assert.equal(reused.unexpectedNetworkFailure?.reason, "login_request_id_reuse");

  const redirected = cancellationTracker();
  registerLoginChain(redirected);
  recordNetworkRequest(redirected, {
    ...loginNetworkEvent("login-request", "auth_login"),
    redirectResponse: { status: 307 },
  });
  assert.equal(redirected.unexpectedNetworkFailure?.reason, "login_redirect");
});


test("login ledger rejects loading failure, non-200, duplicate response and terminal", () => {
  const failed = cancellationTracker();
  registerLoginChain(failed);
  assert.equal(recordNetworkLoadingFailure(failed, {
    requestId: "login-request",
    errorText: "private low-level error must not survive",
  }), "unexpected_login_failure");
  assert.equal(failed.unexpectedNetworkFailure?.reason, "login_loading_failed");
  assert.equal(JSON.stringify(failed).includes("private low-level"), false);

  const badStatus = cancellationTracker();
  registerLoginChain(badStatus);
  recordNetworkResponse(badStatus, { requestId: "login-request", response: { status: 401 } });
  assert.equal(badStatus.unexpectedNetworkFailure?.reason, "login_status");

  const duplicateResponse = cancellationTracker();
  registerLoginChain(duplicateResponse);
  recordNetworkResponse(duplicateResponse, { requestId: "login-request", response: { status: 200 } });
  recordNetworkResponse(duplicateResponse, { requestId: "login-request", response: { status: 200 } });
  assert.equal(duplicateResponse.unexpectedNetworkFailure?.reason, "login_duplicate_response");

  const duplicateTerminal = cancellationTracker();
  registerLoginChain(duplicateTerminal);
  recordNetworkLoadingFinished(duplicateTerminal, { requestId: "login-request" });
  recordNetworkLoadingFinished(duplicateTerminal, { requestId: "login-request" });
  assert.equal(duplicateTerminal.unexpectedNetworkFailure?.reason, "login_duplicate_terminal");
});


test("login completion reports fixed missing-terminal coordinates", () => {
  const tracker = cancellationTracker();
  const attempt = registerLoginChain(tracker);
  recordNetworkResponse(tracker, { requestId: "login-request", response: { status: 200 } });
  assert.throws(
    () => assertLoginAttemptComplete(attempt, "browser.login.finance"),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.login.finance"
      && error.code === "login_terminal_missing",
  );
});


test("login DOM wait fails fast on network faults and a replaced login form", async () => {
  const networkTracker = cancellationTracker();
  const networkAttempt = registerLoginChain(networkTracker);
  recordNetworkLoadingFailure(networkTracker, {
    requestId: "login-request",
    errorText: "net::ERR_FAILED",
  });
  const networkClient = {
    send: async () => ({ result: { value: { ready: false, login_form_returned: false } } }),
  };
  await assert.rejects(
    waitForLoginCompletion(
      networkClient,
      "session",
      networkTracker,
      networkAttempt,
      "browser.login.finance",
      { timeoutMs: 100, pollMs: 1 },
    ),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network.finance_login.auth_login"
      && error.code === "loading_login_loading_failed",
  );

  const formTracker = cancellationTracker();
  const formAttempt = registerLoginChain(formTracker);
  const formClient = {
    send: async () => ({ result: { value: { ready: false, login_form_returned: true } } }),
  };
  await assert.rejects(
    waitForLoginCompletion(
      formClient,
      "session",
      formTracker,
      formAttempt,
      "browser.login.finance",
      { timeoutMs: 100, pollMs: 1 },
    ),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.login.finance"
      && error.code === "form_returned",
  );
});


test("console guard allows only the exact revoked step-up Chromium 403", () => {
  const url = "http://localhost:8001/approvals/apr_private/step-up";
  for (const text of [
    "Failed to load resource: the server responded with a status of 403",
    "Failed to load resource: the server responded with a status of 403 ()",
    "Failed to load resource: the server responded with a status of 403 (Forbidden)",
  ]) {
    assert.equal(
      classifyBrowserConsoleError({ type: "error", text, url, argumentCount: 0 }),
      "expected_step_up_403",
    );
  }
  for (const input of [
    { type: "error", text: "Failed to load resource: the server responded with a status of 403", url: "http://localhost:8001/approvals", argumentCount: 0 },
    { type: "error", text: "Failed to load resource: the server responded with a status of 401", url, argumentCount: 0 },
    { type: "error", text: "Failed to load resource: the server responded with a status of 403 (Forbidden) extra", url, argumentCount: 0 },
    { type: "error", text: "application failure", url: "", argumentCount: 1 },
    { type: "assert", text: "assertion failed", url: "", argumentCount: 1 },
  ]) assert.equal(classifyBrowserConsoleError(input), "unexpected_error");
  assert.equal(
    classifyBrowserConsoleError({ type: "warning", text: "ignored", url, argumentCount: 0 }),
    "ignored",
  );
});


test("Fetch denial is authoritative when Chromium omits the matching 403 Console entry", () => {
  for (const consoleCount of [0, 1]) {
    assert.equal(assertStepUpConsoleLedger({
      interceptedStepUpStatuses: [200, 403],
      expectedStepUpConsoleErrors: consoleCount,
    }), true);
  }
  assert.throws(
    () => assertStepUpConsoleLedger({
      interceptedStepUpStatuses: [200, 403],
      expectedStepUpConsoleErrors: 2,
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "step_up_403_count_over",
  );
  assert.throws(
    () => assertStepUpConsoleLedger({
      interceptedStepUpStatuses: [200],
      expectedStepUpConsoleErrors: 0,
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network"
      && error.code === "step_up_denial_count",
  );
});


test("unexpected console diagnostics retain only fixed reason enums", () => {
  assert.equal(classifyUnexpectedConsoleReason({
    type: "error",
    text: "Failed to load resource: net::ERR_FAILED",
  }), "net_failed");
  assert.equal(classifyUnexpectedConsoleReason({
    type: "error",
    text: "Failed to load resource: net::ERR_ABORTED",
  }), "net_aborted");
  assert.equal(classifyUnexpectedConsoleReason({
    type: "error",
    text: "Failed to load resource: the server responded with a status of 403 (Forbidden)",
  }), "http_4xx");
  assert.equal(classifyUnexpectedConsoleReason({
    type: "error",
    text: "private dynamic detail must not survive",
    source: "runtime",
  }), "runtime_other");
  assert.equal(classifyUnexpectedConsoleReason({
    type: "assert",
    text: "private dynamic detail must not survive",
  }), "runtime_assert");
  assert.equal(
    classifyConsoleUrl("http://localhost:8001/approvals/apr_private/step-up"),
    "approval_step_up",
  );
  assert.equal(classifyConsoleUrl("http://localhost:5173/src/app/main.js"), "console");
  assert.equal(classifyConsoleUrl("http://localhost:8001/auth/login"), "auth_login");
  assert.equal(classifyConsoleUrl("http://localhost:8001/auth/logout"), "auth_logout");
  assert.equal(classifyConsoleUrl("http://localhost:8001/auth/me"), "auth_me");
  assert.equal(classifyConsoleUrl("http://localhost:8001/console/navigation"), "console_navigation");
  assert.equal(classifyConsoleUrl("http://localhost:8001/console/preferences"), "console_preferences");
  assert.equal(classifyConsoleUrl("http://localhost:8001/console/events"), "console_events");
  assert.equal(classifyConsoleUrl("http://localhost:8001/dashboard/role-view"), "dashboard_role_view");
  assert.equal(classifyConsoleUrl("http://localhost:8001/health"), "control_health_summary");
  assert.equal(classifyConsoleUrl("http://localhost:8000/health/live"), "gateway_health_live");
  assert.equal(classifyConsoleUrl("http://localhost:8001/internal/config/summary"), "config_summary");
  assert.equal(classifyConsoleUrl("http://localhost:8001/status/providers"), "control_status");
  assert.equal(classifyConsoleUrl("http://localhost:8001/model-prices"), "control_model_prices");
  assert.equal(classifyConsoleUrl("http://localhost:8001/unknown"), "control_other");
  assert.equal(classifyConsoleUrl("https://example.com/private"), "external");
  assert.equal(classifyConsoleUrl(""), "no_url");
});


test("only exact paused step-up Network ids reconcile Chromium cancellation errors", () => {
  const tracker = cancellationTracker([
    ["network-success", { category: "approval_step_up", method: "POST", phase: "late_success" }],
    ["network-denial", { category: "approval_step_up", method: "POST", phase: "late_denial" }],
  ]);
  assert.deepEqual(
    registerPausedStepUpResponse(
      tracker,
      stepUpFetchEvent("fetch-success", "network-success"),
      200,
      "browser.security.late_success",
    ),
    { requestId: "fetch-success", networkId: "network-success", status: 200 },
  );
  assert.equal(recordNetworkLoadingFailure(tracker, {
    requestId: "network-success",
    canceled: false,
    errorText: "net::ERR_FAILED",
  }), "expected_step_up_cancel");
  assert.deepEqual(
    registerPausedStepUpResponse(
      tracker,
      stepUpFetchEvent("fetch-denial", "network-denial"),
      403,
      "browser.security.late_denial",
    ),
    { requestId: "fetch-denial", networkId: "network-denial", status: 403 },
  );
  assert.equal(recordNetworkLoadingFailure(tracker, {
    requestId: "network-denial",
    canceled: true,
    errorText: "net::ERR_ABORTED",
  }), "expected_step_up_cancel");
  for (const text of [
    "Failed to load resource: net::ERR_FAILED",
    "Failed to load resource: net::ERR_ABORTED",
  ]) {
    assert.equal(recordBrowserConsoleEntry(tracker, {
      type: "error",
      text,
      url: "http://localhost:8001/approvals/apr_private/step-up",
      argumentCount: 0,
      source: "log",
    }), "network_failure_candidate");
  }
  assert.equal(assertCancellationLedger(tracker), true);
  assert.equal(tracker.expectedCanceledRequestIds.size, 0);
  assert.equal(tracker.canceledStepUpRequestCount, 2);
  assert.equal(tracker.unexpectedNetworkFailureCount, 0);
});


test("step-up CORS preflight is exact and remains separate from the business owner ledger", () => {
  assert.equal(
    classifyStepUpPreflight(stepUpPreflightEvent("fetch-preflight", "network-preflight")),
    "valid",
  );
  assert.equal(classifyStepUpPreflight(stepUpFetchEvent("fetch-post", "network-post")), "not_preflight");
  for (const request of [
    { method: "GET" },
    { url: `${CONTROL_ORIGIN}/approvals/apr_contract/step-up?debug=1` },
    { headers: { Origin: "https://external.invalid", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type" } },
    { headers: { Origin: "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization,content-type" } },
    { headers: { Origin: "http://127.0.0.1:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type,x-private" } },
  ]) {
    const event = stepUpPreflightEvent("fetch-invalid", "network-invalid", request);
    assert.equal(
      classifyStepUpPreflight(event),
      String(event.request.method).toUpperCase() === "OPTIONS" ? "invalid" : "not_preflight",
    );
  }
  const tracker = cancellationTracker();
  assert.deepEqual(
    registerStepUpPreflightRequest(
      tracker,
      stepUpPreflightEvent("fetch-preflight", "network-preflight"),
    ),
    {
      category: "approval_step_up",
      method: "OPTIONS",
      phase: "late_success",
      fetchResponseStatus: null,
      networkResponseStatus: null,
    },
  );
  assert.equal(tracker.fetchStepUpOwners.size, 0);
  assert.equal(tracker.stepUpPreflightOwners.size, 1);
});


test("step-up preflight ledger reconciles Fetch and Network response ordering", () => {
  /** 以给定事件顺序运行同一预检 owner，确保 Fetch/Network 反序结果一致。 */
  const runSequence = (order) => {
    const tracker = cancellationTracker();
    const request = stepUpPreflightEvent("fetch-preflight", "network-preflight");
    const response = stepUpPreflightEvent("fetch-preflight-response", "network-preflight", {
      headers: {},
    });
    const actions = {
      fetchRequest: () => registerStepUpPreflightRequest(tracker, request),
      networkRequest: () => recordNetworkRequest(tracker, {
        requestId: "network-preflight",
        request: request.request,
      }),
      fetchResponse: () => registerStepUpPreflightResponse(
        tracker,
        response,
        200,
        "browser.network.preflight",
      ),
      networkResponse: () => recordNetworkResponse(tracker, {
        requestId: "network-preflight",
        response: { status: 200 },
      }),
    };
    for (const name of order) actions[name]();
    assert.equal(
      recordNetworkLoadingFinished(tracker, { requestId: "network-preflight" }),
      "step_up_preflight_finished",
    );
    assert.equal(tracker.stepUpPreflightOwners.size, 0);
    assert.equal(tracker.requestOwners.size, 0);
    assert.equal(tracker.stepUpStatuses.length, 0);
    assert.equal(tracker.unexpectedNetworkFailureCount, 0);
  };
  runSequence(["fetchRequest", "networkRequest", "fetchResponse", "networkResponse"]);
  runSequence(["networkRequest", "fetchRequest", "networkResponse", "fetchResponse"]);
  runSequence(["fetchRequest", "fetchResponse", "networkRequest", "networkResponse"]);
});


test("step-up preflight ledger fails closed on duplicates, drift, redirects and status mismatch", () => {
  const duplicate = cancellationTracker();
  const request = stepUpPreflightEvent("fetch-preflight", "network-preflight");
  registerStepUpPreflightRequest(duplicate, request);
  assert.equal(registerStepUpPreflightRequest(duplicate, request), "invalid");
  assert.equal(duplicate.unexpectedNetworkFailure?.reason, "step_up_preflight_duplicate");

  const missingId = cancellationTracker();
  assert.equal(registerStepUpPreflightRequest(
    missingId,
    stepUpPreflightEvent("", "network-preflight"),
  ), "invalid");
  assert.equal(missingId.unexpectedNetworkFailure?.reason, "step_up_preflight_network_id");

  const mismatch = cancellationTracker();
  mismatch.phase = "fetch_phase";
  registerStepUpPreflightRequest(mismatch, request);
  mismatch.phase = "network_phase";
  recordNetworkRequest(mismatch, { requestId: "network-preflight", request: request.request });
  assert.equal(mismatch.unexpectedNetworkFailure?.reason, "step_up_preflight_owner_mismatch");

  const redirected = cancellationTracker();
  registerStepUpPreflightRequest(redirected, request);
  recordNetworkRequest(redirected, {
    requestId: "network-preflight",
    request: request.request,
    redirectResponse: { status: 307 },
  });
  assert.equal(redirected.unexpectedNetworkFailure?.reason, "step_up_preflight_redirect");

  const badFetchStatus = cancellationTracker();
  registerStepUpPreflightRequest(badFetchStatus, request);
  recordNetworkRequest(badFetchStatus, { requestId: "network-preflight", request: request.request });
  assert.throws(
    () => registerStepUpPreflightResponse(
      badFetchStatus,
      stepUpPreflightEvent("fetch-response", "network-preflight", { headers: {} }),
      204,
      "browser.network.preflight",
    ),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "preflight_status",
  );

  const duplicateResponse = cancellationTracker();
  registerStepUpPreflightRequest(duplicateResponse, request);
  recordNetworkRequest(duplicateResponse, { requestId: "network-preflight", request: request.request });
  const response = stepUpPreflightEvent("fetch-response", "network-preflight", { headers: {} });
  registerStepUpPreflightResponse(
    duplicateResponse,
    response,
    200,
    "browser.network.preflight",
  );
  assert.throws(
    () => registerStepUpPreflightResponse(
      duplicateResponse,
      response,
      200,
      "browser.network.preflight",
    ),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "preflight_duplicate_response",
  );

  const badNetworkStatus = cancellationTracker();
  registerStepUpPreflightRequest(badNetworkStatus, request);
  recordNetworkRequest(badNetworkStatus, { requestId: "network-preflight", request: request.request });
  assert.equal(recordNetworkResponse(badNetworkStatus, {
    requestId: "network-preflight",
    response: { status: 204 },
  }), "unexpected_preflight_response");
  assert.equal(badNetworkStatus.unexpectedNetworkFailure?.reason, "step_up_preflight_response");
});


test("step-up preflight terminal events always clear both ledgers", () => {
  const failed = cancellationTracker();
  const request = stepUpPreflightEvent("fetch-preflight", "network-preflight");
  registerStepUpPreflightRequest(failed, request);
  recordNetworkRequest(failed, { requestId: "network-preflight", request: request.request });
  assert.equal(recordNetworkLoadingFailure(failed, {
    requestId: "network-preflight",
    errorText: "net::ERR_FAILED",
  }), "unexpected_preflight_failure");
  assert.equal(failed.stepUpPreflightOwners.size, 0);
  assert.equal(failed.requestOwners.size, 0);

  const incomplete = cancellationTracker();
  registerStepUpPreflightRequest(incomplete, request);
  recordNetworkRequest(incomplete, { requestId: "network-preflight", request: request.request });
  assert.equal(
    recordNetworkLoadingFinished(incomplete, { requestId: "network-preflight" }),
    "unexpected_preflight_finish",
  );
  assert.equal(incomplete.stepUpPreflightOwners.size, 0);
  assert.equal(incomplete.requestOwners.size, 0);
});


test("health CORS preflight is exact for Gateway and Control and stays outside health attempts", () => {
  for (const port of [8000, 8001]) {
    assert.equal(
      classifyHealthPreflight(healthPreflightEvent("fetch-health", "network-health", { port })),
      "valid",
    );
  }
  assert.equal(
    classifyHealthPreflight(healthRequestEvent("health-get", "health_probe", 1)),
    "not_preflight",
  );
  for (const event of [
    healthPreflightEvent("fetch-invalid", "network-invalid", { pathname: "/health/live?debug=1" }),
    healthPreflightEvent("fetch-invalid", "network-invalid", {
      headers: {
        Origin: "https://external.invalid",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-request-id",
      },
    }),
    healthPreflightEvent("fetch-invalid", "network-invalid", {
      headers: {
        Origin: "http://127.0.0.1:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-request-id",
      },
    }),
    healthPreflightEvent("fetch-invalid", "network-invalid", {
      headers: {
        Origin: "http://127.0.0.1:5173",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization,x-request-id",
      },
    }),
  ]) assert.equal(classifyHealthPreflight(event), "invalid");

  const tracker = cancellationTracker();
  assert.deepEqual(
    registerHealthPreflightRequest(
      tracker,
      healthPreflightEvent("fetch-health", "network-health"),
    ),
    {
      category: "control_health_live",
      method: "OPTIONS",
      phase: "late_success",
      healthTargetKey: "control_localhost.control_health_live",
      consoleOriginKey: "console_loopback",
      networkResponseStatus: null,
      corsResponseAllowed: false,
    },
  );
  assert.equal(tracker.healthRequestLedgers.size, 0);
  assert.equal(tracker.healthRequestIds.size, 0);
  assert.equal(tracker.healthPreflightOwners.size, 1);

  const requestIdHeader = cancellationTracker();
  const optionsWithAttemptHeader = healthPreflightEvent(
    "fetch-health-header",
    "network-health-header",
  );
  optionsWithAttemptHeader.request.headers["X-Request-ID"] = "must_not_register_attempt_1";
  registerHealthPreflightRequest(requestIdHeader, optionsWithAttemptHeader);
  recordNetworkRequest(requestIdHeader, {
    requestId: "network-health-header",
    request: optionsWithAttemptHeader.request,
  });
  assert.equal(attachHealthRequestHeaders(requestIdHeader, "network-health-header", {
    "X-Request-ID": "must_not_register_attempt_1",
  }), "not_health");
  assert.equal(requestIdHeader.healthRequestLedgers.size, 0);
  assert.equal(requestIdHeader.healthRequestIds.size, 0);
});


test("health preflight ledger reconciles Fetch and Network order through response and terminal", () => {
  /** 在 Gateway/Control 两端以指定反序运行健康预检并验证 ledger 归零。 */
  const runSequence = (order, port) => {
    const tracker = cancellationTracker();
    const request = healthPreflightEvent("fetch-health", "network-health", { port });
    const actions = {
      fetchRequest: () => registerHealthPreflightRequest(tracker, request),
      networkRequest: () => recordNetworkRequest(tracker, {
        requestId: "network-health",
        request: request.request,
      }),
      networkResponse: () => recordNetworkResponse(tracker, {
        requestId: "network-health",
        response: { status: 200, headers: healthPreflightResponseHeaders() },
      }),
    };
    for (const name of order) actions[name]();
    assert.equal(
      recordNetworkLoadingFinished(tracker, { requestId: "network-health" }),
      "health_preflight_finished",
    );
    assert.equal(tracker.healthPreflightOwners.size, 0);
    assert.equal(tracker.requestOwners.size, 0);
    assert.equal(tracker.healthProbeFailureCount, 0);
    assert.equal(tracker.healthProbeRecoveryCount, 0);
    assert.equal(tracker.unexpectedNetworkFailureCount, 0);
  };
  runSequence(["fetchRequest", "networkRequest", "networkResponse"], 8000);
  runSequence(["networkRequest", "fetchRequest", "networkResponse"], 8001);
});


test("health preflight ledger fails closed on duplicate, drift, redirect, status and terminal gaps", () => {
  const request = healthPreflightEvent("fetch-health", "network-health");
  const duplicate = cancellationTracker();
  registerHealthPreflightRequest(duplicate, request);
  assert.equal(registerHealthPreflightRequest(duplicate, request), "invalid");
  assert.equal(duplicate.unexpectedNetworkFailure?.reason, "health_preflight_duplicate");

  const missingId = cancellationTracker();
  assert.equal(registerHealthPreflightRequest(
    missingId,
    healthPreflightEvent("", "network-health"),
  ), "invalid");
  assert.equal(missingId.unexpectedNetworkFailure?.reason, "health_preflight_network_id");

  const mismatch = cancellationTracker();
  mismatch.phase = "fetch_phase";
  registerHealthPreflightRequest(mismatch, request);
  mismatch.phase = "network_phase";
  recordNetworkRequest(mismatch, { requestId: "network-health", request: request.request });
  assert.equal(mismatch.unexpectedNetworkFailure?.reason, "health_preflight_owner_mismatch");

  const redirected = cancellationTracker();
  registerHealthPreflightRequest(redirected, request);
  recordNetworkRequest(redirected, {
    requestId: "network-health",
    request: request.request,
    redirectResponse: { status: 307 },
  });
  assert.equal(redirected.unexpectedNetworkFailure?.reason, "health_preflight_redirect");

  const badStatus = cancellationTracker();
  registerHealthPreflightRequest(badStatus, request);
  recordNetworkRequest(badStatus, { requestId: "network-health", request: request.request });
  assert.equal(recordNetworkResponse(badStatus, {
    requestId: "network-health",
    response: { status: 204, headers: healthPreflightResponseHeaders() },
  }), "unexpected_health_preflight_response");
  assert.equal(badStatus.unexpectedNetworkFailure?.reason, "health_preflight_response");

  const failed = cancellationTracker();
  registerHealthPreflightRequest(failed, request);
  recordNetworkRequest(failed, { requestId: "network-health", request: request.request });
  assert.equal(recordNetworkLoadingFailure(failed, {
    requestId: "network-health",
    errorText: "net::ERR_FAILED",
  }), "unexpected_health_preflight_failure");
  assert.equal(failed.healthPreflightOwners.size, 0);
  assert.equal(failed.requestOwners.size, 0);

  const incomplete = cancellationTracker();
  registerHealthPreflightRequest(incomplete, request);
  recordNetworkRequest(incomplete, { requestId: "network-health", request: request.request });
  assert.equal(
    recordNetworkLoadingFinished(incomplete, { requestId: "network-health" }),
    "unexpected_health_preflight_finish",
  );
  assert.equal(incomplete.healthPreflightOwners.size, 0);
  assert.equal(incomplete.requestOwners.size, 0);
});


test("health preflight rejects host alias drift and requires exact CORS response proof", () => {
  const request = healthPreflightEvent("fetch-health", "network-health");
  const aliasDrift = cancellationTracker();
  registerHealthPreflightRequest(aliasDrift, request);
  const aliasNetwork = healthPreflightEvent("unused", "network-health");
  aliasNetwork.request.url = "http://127.0.0.1:8001/health/live";
  recordNetworkRequest(aliasDrift, { requestId: "network-health", request: aliasNetwork.request });
  assert.equal(aliasDrift.unexpectedNetworkFailure?.reason, "health_preflight_owner_mismatch");

  for (const headers of [
    {},
    healthPreflightResponseHeaders("http://localhost:5173"),
    {
      "Access-Control-Allow-Origin": "http://127.0.0.1:5173",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "X-Request-ID",
    },
    {
      "Access-Control-Allow-Origin": "http://127.0.0.1:5173",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  ]) {
    const tracker = cancellationTracker();
    registerHealthPreflightRequest(tracker, request);
    recordNetworkRequest(tracker, { requestId: "network-health", request: request.request });
    assert.equal(recordNetworkResponse(tracker, {
      requestId: "network-health",
      response: { status: 200, headers },
    }), "unexpected_health_preflight_response");
    assert.equal(tracker.unexpectedNetworkFailure?.reason, "health_preflight_response");
  }

  const duplicateResponse = cancellationTracker();
  registerHealthPreflightRequest(duplicateResponse, request);
  recordNetworkRequest(duplicateResponse, { requestId: "network-health", request: request.request });
  const response = {
    requestId: "network-health",
    response: { status: 200, headers: healthPreflightResponseHeaders() },
  };
  assert.equal(recordNetworkResponse(duplicateResponse, response), "health_preflight_response");
  assert.equal(recordNetworkResponse(duplicateResponse, response), "unexpected_health_preflight_response");
  assert.equal(duplicateResponse.unexpectedNetworkFailure?.reason, "health_preflight_response");

  const networkOnly = cancellationTracker();
  recordNetworkRequest(networkOnly, { requestId: "network-health", request: request.request });
  assert.equal(recordNetworkResponse(networkOnly, response), "unexpected_health_preflight_response");
  assert.equal(networkOnly.unexpectedNetworkFailure?.reason, "health_preflight_response");
});


test("step-up Fetch owner reconciles request, response and Network events in either order", () => {
  const fetchFirst = cancellationTracker();
  const fetchFirstEvent = stepUpFetchEvent("fetch-first", "network-first");
  assert.deepEqual(registerFetchStepUpOwner(fetchFirst, fetchFirstEvent), {
    category: "approval_step_up",
    method: "POST",
    phase: "late_success",
  });
  assert.deepEqual(
    registerPausedStepUpResponse(
      fetchFirst,
      fetchFirstEvent,
      200,
      "browser.security.late_success",
    ),
    { requestId: "fetch-first", networkId: "network-first", status: 200 },
  );
  recordNetworkRequest(fetchFirst, stepUpNetworkEvent("network-first"));
  assert.equal(recordNetworkLoadingFailure(fetchFirst, {
    requestId: "network-first",
    errorText: "net::ERR_FAILED",
  }), "expected_step_up_cancel");
  assert.equal(fetchFirst.fetchStepUpOwners.size, 0);
  assert.equal(fetchFirst.requestOwners.size, 0);

  const networkFirst = cancellationTracker();
  recordNetworkRequest(networkFirst, stepUpNetworkEvent("network-first"));
  assert.deepEqual(
    registerFetchStepUpOwner(
      networkFirst,
      stepUpFetchEvent("fetch-first", "network-first"),
    ),
    { category: "approval_step_up", method: "POST", phase: "late_success" },
  );
  registerPausedStepUpResponse(
    networkFirst,
    stepUpFetchEvent("fetch-first", "network-first"),
    200,
    "browser.security.late_success",
  );
  assert.equal(recordNetworkLoadingFailure(networkFirst, {
    requestId: "network-first",
    errorText: "net::ERR_ABORTED",
  }), "expected_step_up_cancel");
  assert.equal(networkFirst.fetchStepUpOwners.size, 0);
  assert.equal(networkFirst.requestOwners.size, 0);
});


test("step-up Fetch owner rejects invalid identity, duplicates and Network conflicts", () => {
  const missingId = cancellationTracker();
  assert.equal(registerFetchStepUpOwner(
    missingId,
    stepUpFetchEvent("fetch-missing", ""),
  ), "invalid");
  assert.equal(missingId.unexpectedNetworkFailure?.reason, "step_up_network_id");

  const missingFetchId = cancellationTracker();
  assert.equal(registerFetchStepUpOwner(
    missingFetchId,
    stepUpFetchEvent("", "network-missing-fetch"),
  ), "invalid");
  assert.equal(missingFetchId.unexpectedNetworkFailure?.reason, "step_up_network_id");

  const wrongMethod = cancellationTracker();
  assert.equal(registerFetchStepUpOwner(
    wrongMethod,
    stepUpFetchEvent("fetch-get", "network-get", { method: "GET" }),
  ), "invalid");
  assert.equal(wrongMethod.unexpectedNetworkFailure?.reason, "step_up_method");

  const wrongRoute = cancellationTracker();
  assert.equal(registerFetchStepUpOwner(
    wrongRoute,
    stepUpFetchEvent("fetch-detail", "network-detail", { pathname: "/approvals/apr_contract" }),
  ), "ignored");
  assert.equal(wrongRoute.fetchStepUpOwners.size, 0);
  assert.equal(registerFetchStepUpOwner(
    wrongRoute,
    stepUpFetchEvent("fetch-query", "network-query", {
      pathname: "/approvals/apr_contract/step-up?debug=1",
    }),
  ), "invalid");
  assert.equal(wrongRoute.unexpectedNetworkFailure?.reason, "step_up_route");

  const duplicate = cancellationTracker();
  const duplicateEvent = stepUpFetchEvent("fetch-duplicate", "network-duplicate");
  registerFetchStepUpOwner(duplicate, duplicateEvent);
  assert.equal(registerFetchStepUpOwner(duplicate, duplicateEvent), "invalid");
  assert.equal(duplicate.unexpectedNetworkFailure?.reason, "step_up_duplicate_hint");

  const mismatch = cancellationTracker();
  mismatch.phase = "fetch_phase";
  registerFetchStepUpOwner(mismatch, stepUpFetchEvent("fetch-mismatch", "network-mismatch"));
  mismatch.phase = "network_phase";
  recordNetworkRequest(mismatch, stepUpNetworkEvent("network-mismatch"));
  assert.equal(mismatch.unexpectedNetworkFailure?.reason, "step_up_owner_mismatch");

  const redirected = cancellationTracker();
  registerFetchStepUpOwner(
    redirected,
    stepUpFetchEvent("fetch-redirect", "network-redirect"),
  );
  recordNetworkRequest(redirected, {
    ...stepUpNetworkEvent("network-redirect"),
    redirectResponse: { status: 307 },
  });
  assert.equal(redirected.unexpectedNetworkFailure?.reason, "step_up_redirect");

  const responseRoute = cancellationTracker([
    ["network-route", { category: "approval_step_up", method: "POST", phase: "late_success" }],
  ]);
  assert.throws(
    () => registerPausedStepUpResponse(
      responseRoute,
      stepUpFetchEvent("fetch-route", "network-route", { pathname: "/approvals/apr_contract" }),
      200,
      "browser.security.late_success",
    ),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "network_route",
  );
  assert.throws(
    () => registerPausedStepUpResponse(
      responseRoute,
      stepUpFetchEvent("fetch-method", "network-route", { method: "GET" }),
      200,
      "browser.security.late_success",
    ),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "network_method",
  );

  const duplicateResponse = cancellationTracker([
    ["network-response", { category: "approval_step_up", method: "POST", phase: "late_success" }],
  ]);
  const responseEvent = stepUpFetchEvent("fetch-response", "network-response");
  registerPausedStepUpResponse(
    duplicateResponse,
    responseEvent,
    200,
    "browser.security.late_success",
  );
  assert.throws(
    () => registerPausedStepUpResponse(
      duplicateResponse,
      responseEvent,
      200,
      "browser.security.late_success",
    ),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "network_duplicate",
  );
});


test("step-up Fetch owner is cleared by both Network terminal events", () => {
  const failed = cancellationTracker();
  const failedEvent = stepUpFetchEvent("fetch-failed", "network-failed");
  registerFetchStepUpOwner(failed, failedEvent);
  recordNetworkRequest(failed, stepUpNetworkEvent("network-failed"));
  registerPausedStepUpResponse(
    failed,
    failedEvent,
    200,
    "browser.security.late_success",
  );
  assert.equal(recordNetworkLoadingFailure(failed, {
    requestId: "network-failed",
    errorText: "net::ERR_FAILED",
  }), "expected_step_up_cancel");
  assert.equal(failed.fetchStepUpOwners.size, 0);
  assert.equal(failed.requestOwners.size, 0);
  assert.equal(failed.expectedCanceledRequestIds.size, 0);

  const finished = cancellationTracker();
  const finishedEvent = stepUpFetchEvent("fetch-finished", "network-finished");
  registerFetchStepUpOwner(finished, finishedEvent);
  recordNetworkRequest(finished, stepUpNetworkEvent("network-finished"));
  registerPausedStepUpResponse(
    finished,
    finishedEvent,
    200,
    "browser.security.late_success",
  );
  assert.equal(
    recordNetworkLoadingFinished(finished, { requestId: "network-finished" }),
    "unexpected_finish",
  );
  assert.equal(finished.fetchStepUpOwners.size, 0);
  assert.equal(finished.requestOwners.size, 0);
  assert.equal(finished.expectedCanceledRequestIds.size, 0);
});


test("cancellation attribution rejects wrong ids, wrong owners and non-cancel failures", () => {
  const wrongOwner = cancellationTracker([
    ["network-detail", { category: "approval_detail", method: "GET", phase: "late_success" }],
  ]);
  assert.throws(
    () => registerPausedStepUpResponse(
      wrongOwner,
      stepUpFetchEvent("fetch-detail", "network-detail"),
      200,
      "browser.security.late_success",
    ),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "network_owner",
  );

  const wrongId = cancellationTracker([
    ["network-expected", { category: "approval_step_up", method: "POST", phase: "late_success" }],
    ["network-other", { category: "approval_step_up", method: "POST", phase: "late_success" }],
  ]);
  registerPausedStepUpResponse(
    wrongId,
    stepUpFetchEvent("fetch-expected", "network-expected"),
    200,
    "browser.security.late_success",
  );
  assert.equal(recordNetworkLoadingFailure(wrongId, {
    requestId: "network-other",
    errorText: "net::ERR_FAILED",
  }), "unexpected_failure");
  assert.equal(wrongId.canceledStepUpRequestCount, 0);
  assert.equal(wrongId.expectedCanceledRequestIds.has("network-expected"), true);
  assert.throws(
    () => assertCancellationLedger(wrongId),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network.late_success.approval_step_up"
      && error.code === "loading_failed",
  );

  const connection = cancellationTracker([
    ["network-connect", { category: "approval_step_up", method: "POST", phase: "late_denial" }],
  ]);
  registerPausedStepUpResponse(
    connection,
    stepUpFetchEvent("fetch-connect", "network-connect"),
    403,
    "browser.security.late_denial",
  );
  recordNetworkLoadingFailure(connection, {
    requestId: "network-connect",
    errorText: "net::ERR_CONNECTION_REFUSED",
  });
  assert.equal(connection.canceledStepUpRequestCount, 0);
  assert.equal(connection.unexpectedNetworkFailure?.reason, "connection_refused");
});


test("registered cancellations reject normal completion and extra Console errors", () => {
  const finished = cancellationTracker([
    ["network-finished", { category: "approval_step_up", method: "POST", phase: "late_success" }],
  ]);
  registerPausedStepUpResponse(
    finished,
    stepUpFetchEvent("fetch-finished", "network-finished"),
    200,
    "browser.security.late_success",
  );
  assert.equal(
    recordNetworkLoadingFinished(finished, { requestId: "network-finished" }),
    "unexpected_finish",
  );
  assert.equal(finished.unexpectedNetworkFailure?.reason, "finished");

  const extraConsole = cancellationTracker();
  extraConsole.canceledStepUpRequestCount = 2;
  for (let index = 0; index < 3; index += 1) {
    recordBrowserConsoleEntry(extraConsole, {
      type: "error",
      text: "Failed to load resource: net::ERR_FAILED",
      url: "http://localhost:8001/approvals/apr_private/step-up",
      argumentCount: 0,
      source: "log",
    });
  }
  assert.throws(
    () => assertCancellationLedger(extraConsole),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "network_failure_count_over",
  );
  assert.equal(recordBrowserConsoleEntry(extraConsole, {
    type: "error",
    text: "application failure",
    url: "http://localhost:5173/main.js",
    argumentCount: 0,
    source: "log",
  }), "unexpected_error");
  assert.equal(extraConsole.unexpectedConsoleErrors, 1);
});


test("Network proof remains authoritative when Chromium omits cancellation Console entries", () => {
  for (const consoleCount of [0, 1]) {
    const tracker = cancellationTracker();
    tracker.canceledStepUpRequestCount = 2;
    for (let index = 0; index < consoleCount; index += 1) {
      assert.equal(recordBrowserConsoleEntry(tracker, {
        type: "error",
        text: "Failed to load resource: net::ERR_FAILED",
        url: "http://localhost:8001/approvals/apr_private/step-up",
        argumentCount: 0,
        source: "log",
      }), "network_failure_candidate");
    }
    assert.equal(assertCancellationLedger(tracker), true);
  }
});


test("health attempt ids are strict and only exact routes participate in recovery", () => {
  assert.deepEqual(parseHealthAttemptRequestId("gateway_live_1_probe_2_attempt_1"), {
    logicalId: "gateway_live_1_probe_2",
    attempt: 1,
  });
  assert.deepEqual(parseHealthAttemptRequestId("control.ready:4_attempt_2"), {
    logicalId: "control.ready:4",
    attempt: 2,
  });
  for (const value of [
    "", "_attempt_1", "health_attempt_0", "health_attempt_3", "health_attempt_02",
    "health attempt_1", `${"x".repeat(241)}_attempt_1`,
  ]) assert.equal(parseHealthAttemptRequestId(value), null);

  const ordinary = cancellationTracker([[
    "auth-failed",
    { category: "allowed", method: "GET", phase: "finance" },
  ]]);
  recordNetworkLoadingFailure(ordinary, {
    requestId: "auth-failed",
    errorText: "net::ERR_CONNECTION_REFUSED",
  });
  assert.equal(ordinary.healthProbeFailureCount, 0);
  assert.equal(ordinary.unexpectedNetworkFailure?.reason, "connection_refused");
});


test("health owner accepts request headers from Network ExtraInfo or Fetch in either order", () => {
  const networkFirst = cancellationTracker();
  const provisional = healthRequestEvent("network-first", "network_probe", 1);
  provisional.request.headers = {};
  recordNetworkRequest(networkFirst, provisional);
  assert.equal(networkFirst.healthRequestLedgers.size, 0);
  assert.equal(attachHealthRequestHeaders(networkFirst, "network-first", {
    "X-Request-ID": "network_probe_attempt_1",
  }), "attached");
  assert.equal(networkFirst.healthRequestLedgers.has("network_probe"), true);
  assert.equal(attachHealthRequestHeaders(networkFirst, "network-first", {
    "x-request-id": "network_probe_attempt_1",
  }), "already_attached");

  const extraFirst = cancellationTracker();
  assert.equal(attachHealthRequestHeaders(extraFirst, "extra-first", {
    "X-Request-ID": "extra_probe_attempt_1",
  }), "deferred");
  const delayedOwner = healthRequestEvent("extra-first", "ignored_by_event", 1);
  delayedOwner.request.headers = {};
  recordNetworkRequest(extraFirst, delayedOwner);
  assert.equal(extraFirst.healthRequestHeaderHints.size, 0);
  assert.equal(extraFirst.healthRequestLedgers.has("extra_probe"), true);

  assert.equal(attachHealthRequestHeaders(extraFirst, "extra-first", {
    "X-Request-ID": "different_probe_attempt_1",
  }), "mismatch");
  assert.equal(extraFirst.unexpectedNetworkFailure?.reason, "health_attempt_mismatch");

  const missing = cancellationTracker();
  const noHeader = healthRequestEvent("missing", "missing_probe", 1);
  noHeader.request.headers = {};
  recordNetworkRequest(missing, noHeader);
  assert.equal(recordNetworkLoadingFailure(missing, {
    requestId: "missing",
    errorText: "net::ERR_FAILED",
  }), "unexpected_failure");
  assert.equal(missing.unexpectedNetworkFailure?.reason, "health_attempt_id");
});


test("health recovery requires same logical route, 200 response and loading completion", () => {
  const tracker = cancellationTracker();
  tracker.phase = "late_success_return";
  recordNetworkRequest(tracker, healthRequestEvent(
    "health-first",
    "gateway_live_probe_1",
    1,
  ));
  assert.equal(recordNetworkLoadingFailure(tracker, {
    requestId: "health-first",
    errorText: "net::ERR_FAILED",
  }), "recoverable_health_failure");
  assert.equal(tracker.healthRequestLedgers.get("gateway_live_probe_1").state, "pending");
  recordNetworkRequest(tracker, healthRequestEvent(
    "health-retry",
    "gateway_live_probe_1",
    2,
  ));
  assert.equal(recordNetworkResponse(tracker, {
    requestId: "health-retry",
    response: { status: 200 },
  }), "recorded");
  assert.equal(tracker.healthProbeRecoveryCount, 0);
  assert.equal(recordNetworkLoadingFinished(tracker, {
    requestId: "health-retry",
  }), "health_retry_finished");
  assert.equal(tracker.healthProbeRecoveryCount, 1);
  assert.equal(tracker.healthRequestLedgers.get("gateway_live_probe_1").state, "recovered");
  tracker.canceledStepUpRequestCount = 2;
  tracker.consoleNetworkFailureCount = 3;
  assert.equal(assertCancellationLedger(tracker), true);
});


test("health recovery supports reversed CDP ordering but rejects mismatches and retry failures", () => {
  const reversed = cancellationTracker();
  reversed.phase = "security";
  recordNetworkRequest(reversed, healthRequestEvent(
    "health-retry-first",
    "control_ready_probe_7",
    2,
    "control_health_ready",
  ));
  recordNetworkResponse(reversed, {
    requestId: "health-retry-first",
    response: { status: 200 },
  });
  recordNetworkLoadingFinished(reversed, { requestId: "health-retry-first" });
  assert.equal(reversed.healthRequestLedgers.get("control_ready_probe_7").state, "pending");
  recordNetworkRequest(reversed, healthRequestEvent(
    "health-failure-late",
    "control_ready_probe_7",
    1,
    "control_health_ready",
  ));
  recordNetworkLoadingFailure(reversed, {
    requestId: "health-failure-late",
    errorText: "net::ERR_CONNECTION_REFUSED",
  });
  assert.equal(reversed.healthProbeRecoveryCount, 1);
  assert.equal(reversed.healthRequestLedgers.get("control_ready_probe_7").state, "recovered");

  for (const owner of [
    {
      category: "gateway_health_ready",
      method: "GET",
      phase: "finance",
      healthAttempt: null,
    },
    {
      category: "gateway_health_ready",
      method: "POST",
      phase: "finance",
      healthAttempt: { logicalId: "gateway_ready_probe_2", attempt: 1 },
    },
    {
      category: "gateway_health_ready",
      method: "GET",
      phase: "finance",
      healthAttempt: { logicalId: "gateway_ready_probe_2", attempt: 2 },
    },
  ]) {
    const failed = cancellationTracker([["failed", owner]]);
    recordNetworkLoadingFailure(failed, {
      requestId: "failed",
      errorText: "net::ERR_FAILED",
    });
    assert.equal(failed.healthProbeFailureCount, 0);
    assert.equal(failed.unexpectedNetworkFailureCount, 1);
  }

  const wrongRoute = cancellationTracker();
  wrongRoute.phase = "finance";
  recordNetworkRequest(wrongRoute, healthRequestEvent("same-first", "same-logical", 1));
  recordNetworkLoadingFailure(wrongRoute, {
    requestId: "same-first",
    errorText: "net::ERR_FAILED",
  });
  recordNetworkRequest(wrongRoute, healthRequestEvent(
    "wrong-route",
    "same-logical",
    2,
    "gateway_health_ready",
  ));
  recordNetworkResponse(wrongRoute, { requestId: "wrong-route", response: { status: 200 } });
  recordNetworkLoadingFinished(wrongRoute, { requestId: "wrong-route" });
  assert.equal(wrongRoute.healthProbeRecoveryCount, 0);
  assert.equal(wrongRoute.healthRequestLedgers.get("same-logical").state, "invalid");
  assert.equal(wrongRoute.unexpectedNetworkFailure?.reason, "health_route");

  const badStatus = cancellationTracker();
  badStatus.phase = "finance";
  recordNetworkRequest(badStatus, healthRequestEvent(
    "bad-first",
    "control_version_probe_2",
    1,
    "control_health_version",
  ));
  recordNetworkLoadingFailure(badStatus, {
    requestId: "bad-first",
    errorText: "net::ERR_CONNECTION_REFUSED",
  });
  recordNetworkRequest(badStatus, healthRequestEvent(
    "bad-status",
    "control_version_probe_2",
    2,
    "control_health_version",
  ));
  recordNetworkResponse(badStatus, { requestId: "bad-status", response: { status: 503 } });
  assert.equal(recordNetworkLoadingFinished(badStatus, { requestId: "bad-status" }), "health_retry_finished");
  assert.equal(badStatus.unexpectedNetworkFailure?.reason, "health_status");
});


test("health ledger rejects redundant retry, duplicate attempt, request id reuse and redirects", () => {
  const redundant = cancellationTracker();
  recordNetworkRequest(redundant, healthRequestEvent("first", "healthy_probe", 1));
  recordNetworkResponse(redundant, { requestId: "first", response: { status: 200 } });
  recordNetworkLoadingFinished(redundant, { requestId: "first" });
  assert.equal(redundant.healthRequestLedgers.get("healthy_probe").state, "succeeded");
  recordNetworkRequest(redundant, healthRequestEvent("retry", "healthy_probe", 2));
  assert.equal(redundant.healthRequestLedgers.get("healthy_probe").state, "invalid");
  assert.equal(redundant.unexpectedNetworkFailure?.reason, "health_redundant_retry");

  const duplicate = cancellationTracker();
  recordNetworkRequest(duplicate, healthRequestEvent("first-a", "duplicate_probe", 1));
  recordNetworkRequest(duplicate, healthRequestEvent("first-b", "duplicate_probe", 1));
  assert.equal(duplicate.unexpectedNetworkFailure?.reason, "health_duplicate_attempt");

  const reused = cancellationTracker();
  recordNetworkRequest(reused, healthRequestEvent("same-id", "reuse_probe", 1));
  recordNetworkRequest(reused, healthRequestEvent("same-id", "other_probe", 1));
  assert.equal(reused.unexpectedNetworkFailure?.reason, "request_id_reuse");

  const redirected = cancellationTracker();
  recordNetworkRequest(redirected, healthRequestEvent("redirect-id", "redirect_probe", 1));
  recordNetworkRequest(redirected, {
    ...healthRequestEvent("redirect-id", "redirect_probe", 1),
    redirectResponse: { status: 307 },
  });
  assert.equal(redirected.healthRequestLedgers.get("redirect_probe").state, "invalid");
  assert.equal(redirected.unexpectedNetworkFailure?.reason, "health_redirect");
});


test("health ledger reconciles loadingFinished before response but requires both", () => {
  const tracker = cancellationTracker();
  recordNetworkRequest(tracker, healthRequestEvent("finish-first", "finish_probe", 1));
  assert.equal(
    recordNetworkLoadingFinished(tracker, { requestId: "finish-first" }),
    "health_finished_waiting_status",
  );
  assert.equal(tracker.requestOwners.has("finish-first"), true);
  assert.equal(recordNetworkResponse(tracker, {
    requestId: "finish-first",
    response: { status: 200 },
  }), "health_response_finished");
  assert.equal(tracker.requestOwners.has("finish-first"), false);
  assert.equal(tracker.healthRequestLedgers.get("finish_probe").state, "succeeded");

  assert.equal(
    recordNetworkLoadingFinished(tracker, { requestId: "finish-first" }),
    "unexpected_health_terminal",
  );
  assert.equal(tracker.unexpectedNetworkFailure?.reason, "health_unowned_terminal");
});


test("Console network candidates may be omitted but cannot exceed Network proof", () => {
  const tracker = cancellationTracker();
  tracker.canceledStepUpRequestCount = 2;
  tracker.healthProbeFailureCount = 1;
  tracker.healthProbeRecoveryCount = 1;
  tracker.healthRequestLedgers.set("recovered", { state: "recovered" });
  tracker.consoleNetworkFailureCount = 2;
  assert.equal(assertCancellationLedger(tracker), true);
  tracker.consoleNetworkFailureCount = 4;
  assert.throws(
    () => assertCancellationLedger(tracker),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "network_failure_count_over",
  );
});


test("completion schema is exact and every lifecycle fact must be proven", () => {
  const facts = completeFacts();
  assert.equal(validateCompletionFacts(facts), facts);
  const falseLifecycle = structuredClone(facts);
  falseLifecycle.lifecycle.late_denial_discarded = false;
  assert.throws(() => validateCompletionFacts(falseLifecycle), BrowserAcceptanceFailure);
  const overflow = structuredClone(facts);
  overflow.mobile.horizontal_overflow = true;
  assert.throws(() => validateCompletionFacts(overflow), BrowserAcceptanceFailure);
  const extra = structuredClone(facts);
  extra.page_text = "must-not-be-accepted";
  assert.throws(() => validateCompletionFacts(extra), BrowserAcceptanceFailure);
  const wrongScreenshot = structuredClone(facts);
  wrongScreenshot.screenshots[2] = "outside.png";
  assert.throws(() => validateCompletionFacts(wrongScreenshot), BrowserAcceptanceFailure);
});


test("Windows browser cleanup uses only the exact spawned process tree", async () => {
  const calls = [];
  const pageSession = { client: { close: () => calls.push("page.close") } };
  const browserClient = {
    close: () => calls.push("browser.close"),
    send: async () => calls.push("browser.send"),
  };
  const child = { exitCode: null, pid: 12345 };
  const result = await closeBrowser(browserClient, pageSession, child, {
    platform: "win32",
    terminateOperation: async (target) => calls.push(`tree:${target.pid}`),
  });
  assert.deepEqual(calls, ["page.close", "browser.close", "tree:12345"]);
  assert.deepEqual(result, { treeDrained: true, stdioEof: true, forced: true });
});


test("Windows tree killer validates PID and rejects taskkill non-zero exit", async () => {
  await assert.rejects(
    terminateWindowsProcessTree(0),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "pid",
  );
  const killer = new EventEmitter();
  killer.exitCode = null;
  killer.kill = () => true;
  const attempt = terminateWindowsProcessTree(12345, {
    spawnOperation: () => {
      queueMicrotask(() => {
        killer.exitCode = 5;
        killer.emit("exit", 5);
      });
      return killer;
    },
    timeoutMs: 100,
  });
  await assert.rejects(
    attempt,
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "exit",
  );
});


test("logout waits for a stable empty tracked-network window", async () => {
  const tracker = {
    networkActivityRevision: 1,
    requestOwners: new Map([["navigation", { method: "GET" }]]),
    healthRequestLedgers: new Map(),
  };
  setTimeout(() => {
    tracker.requestOwners.delete("navigation");
    tracker.networkActivityRevision += 1;
  }, 5);
  setTimeout(() => {
    tracker.networkActivityRevision += 1;
  }, 15);
  const started = Date.now();
  assert.equal(await waitForRequestOwnersDrain(
    tracker,
    "browser.network.unit",
    { timeoutMs: 200, stableMs: 20, pollMs: 2 },
  ), true);
  assert.equal(Date.now() - started >= 30, true);

  await assert.rejects(
    waitForRequestOwnersDrain(
      {
        networkActivityRevision: 1,
        requestOwners: new Map([["stuck", { method: "GET" }]]),
        healthRequestLedgers: new Map(),
      },
      "browser.network.unit_timeout",
      { timeoutMs: 15, stableMs: 5, pollMs: 2 },
    ),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network.unit_timeout"
      && error.code === "owners_timeout",
  );
});


test("overview drain ignores only the exact paused step-up and waits for ordinary reads", async () => {
  const tracker = cancellationTracker([
    ["paused", { category: "approval_step_up", method: "POST", phase: "late_denial" }],
    ["config", { category: "config_summary", method: "GET", phase: "late_denial" }],
  ]);
  tracker.phase = "late_denial";
  tracker.fetchStepUpOwners.set(
    "paused",
    { category: "approval_step_up", method: "POST", phase: "late_denial" },
  );
  tracker.expectedCanceledRequestIds.add("paused");
  tracker.pausedResponse = { requestId: "fetch-paused", networkId: "paused", status: 403 };
  setTimeout(() => {
    tracker.requestOwners.delete("config");
    tracker.networkActivityRevision += 1;
  }, 10);
  const started = Date.now();
  assert.equal(await waitForRequestOwnersDrain(
    tracker,
    "browser.network.paused_step_up_unit",
    { timeoutMs: 200, stableMs: 20, pollMs: 2, allowPausedStepUp: true },
  ), true);
  assert.equal(Date.now() - started >= 25, true);

  // AbortController 可先让 Network 账本完整结算；Fetch 控制句柄仍需稍后显式释放。
  tracker.requestOwners.delete("paused");
  tracker.fetchStepUpOwners.delete("paused");
  tracker.expectedCanceledRequestIds.delete("paused");
  assert.equal(await waitForRequestOwnersDrain(
    tracker,
    "browser.network.paused_step_up_terminal",
    { timeoutMs: 100, stableMs: 5, pollMs: 2, allowPausedStepUp: true },
  ), true);

  tracker.requestOwners.set(
    "paused",
    { category: "approval_step_up", method: "POST", phase: "late_denial" },
  );
  tracker.fetchStepUpOwners.set(
    "paused",
    { category: "config_summary", method: "GET", phase: "late_denial" },
  );
  tracker.expectedCanceledRequestIds.add("paused");
  await assert.rejects(
    waitForRequestOwnersDrain(
      tracker,
      "browser.network.paused_step_up_invalid",
      { timeoutMs: 50, stableMs: 5, pollMs: 2, allowPausedStepUp: true },
    ),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network.paused_step_up_invalid"
      && error.code === "paused_owner_invalid",
  );
});


test("network drain waits across the empty window between health retry attempts", async () => {
  const tracker = {
    networkActivityRevision: 3,
    requestOwners: new Map(),
    healthRequestLedgers: new Map([["control_ready_probe", {
      category: "control_health_ready",
      phase: "security_logout_prepare",
      state: "pending",
    }]]),
  };
  setTimeout(() => {
    tracker.requestOwners.set("attempt-2", { method: "GET" });
    tracker.networkActivityRevision += 1;
  }, 5);
  setTimeout(() => {
    tracker.requestOwners.delete("attempt-2");
    tracker.healthRequestLedgers.get("control_ready_probe").state = "recovered";
    tracker.networkActivityRevision += 1;
  }, 15);
  const started = Date.now();
  assert.equal(await waitForRequestOwnersDrain(
    tracker,
    "browser.network.health_retry_unit",
    { timeoutMs: 200, stableMs: 20, pollMs: 2 },
  ), true);
  assert.equal(Date.now() - started >= 30, true);
});


test("network drain waits for reversed health CDP events to reconcile", async () => {
  const tracker = {
    networkActivityRevision: 5,
    requestOwners: new Map(),
    healthRequestLedgers: new Map([["gateway_live_probe", {
      category: "gateway_health_live",
      phase: "security_final",
      state: "pending",
    }]]),
  };
  setTimeout(() => {
    tracker.healthRequestLedgers.get("gateway_live_probe").state = "recovered";
    tracker.networkActivityRevision += 1;
  }, 15);
  const started = Date.now();
  assert.equal(await waitForRequestOwnersDrain(
    tracker,
    "browser.network.health_reverse_unit",
    { timeoutMs: 200, stableMs: 20, pollMs: 2 },
  ), true);
  assert.equal(Date.now() - started >= 30, true);

  await assert.rejects(
    waitForRequestOwnersDrain({
      networkActivityRevision: 1,
      requestOwners: new Map(),
      healthRequestLedgers: new Map([["stuck", {
        category: "control_health_live",
        state: "pending",
      }]]),
    }, "browser.network.health_reverse_timeout", { timeoutMs: 15, stableMs: 5, pollMs: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network.health_reverse_timeout"
      && error.code === "health_ledger_timeout",
  );
});


test("network drain waits for a health preflight ledger terminal", async () => {
  const tracker = {
    networkActivityRevision: 1,
    requestOwners: new Map(),
    healthRequestLedgers: new Map(),
    healthPreflightOwners: new Map([["preflight", { category: "control_health_live" }]]),
  };
  setTimeout(() => {
    tracker.healthPreflightOwners.delete("preflight");
    tracker.networkActivityRevision += 1;
  }, 15);
  const started = Date.now();
  assert.equal(await waitForRequestOwnersDrain(
    tracker,
    "browser.network.health_preflight_unit",
    { timeoutMs: 200, stableMs: 20, pollMs: 2 },
  ), true);
  assert.equal(Date.now() - started >= 30, true);

  await assert.rejects(
    waitForRequestOwnersDrain({
      networkActivityRevision: 1,
      requestOwners: new Map(),
      healthRequestLedgers: new Map(),
      healthPreflightOwners: new Map([["stuck", { category: "gateway_health_live" }]]),
    }, "browser.network.health_preflight_timeout", { timeoutMs: 15, stableMs: 5, pollMs: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network.health_preflight_timeout"
      && error.code === "owners_timeout",
  );
});


test("network drain reports a fixed invalid-ledger coordinate without waiting for timeout", async () => {
  const tracker = cancellationTracker();
  tracker.phase = "security_final";
  recordNetworkRequest(tracker, healthRequestEvent("first", "invalid_probe", 1));
  recordNetworkResponse(tracker, { requestId: "first", response: { status: 200 } });
  recordNetworkLoadingFinished(tracker, { requestId: "first" });
  recordNetworkRequest(tracker, healthRequestEvent("retry", "invalid_probe", 2));
  const started = Date.now();
  await assert.rejects(
    waitForRequestOwnersDrain(
      tracker,
      "browser.network.invalid_ledger",
      { timeoutMs: 1_000, stableMs: 5, pollMs: 2 },
    ),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network.security_final.gateway_health_live"
      && error.code === "loading_health_redundant_retry",
  );
  assert.equal(Date.now() - started < 100, true);
});


test("source contains real modal, owner drift, late response, logout and backend revocation flows", async () => {
  const source = await readFile(path.join(ROOT, "scripts", "acceptance", "prd078-browser-acceptance.mjs"), "utf8");
  for (const required of [
    ":modal",
    "Input.dispatchKeyEvent",
    "Input.dispatchMouseEvent",
    "pauseNextStepUp(tracker, 200",
    "pauseNextStepUp(tracker, 403",
    '"browser.security.owner_drift.overview_stable"',
    '"browser.security.revoke.overview_stable"',
    "allowPausedStepUp: true",
    'waitForRequestOwnersDrain(tracker, "browser.security.late_success.idle")',
    'waitForRequestOwnersDrain(tracker, "browser.security.late_denial.idle")',
    'window.__prd078OverviewRefreshRoot = root',
    'root !== window.__prd078OverviewRefreshRoot',
    'delete window.__prd078OverviewRefreshRoot',
    'waitForRequestOwnersDrain(tracker, "browser.security.revoke_refresh.idle")',
    "Page.addScriptToEvaluateOnNewDocument",
    "Page.removeScriptToEvaluateOnNewDocument",
    "installBrowserClockGate(",
    "restoreBrowserClockGate(",
    "[data-role-dashboard-root][data-role-dashboard-revision]",
    'postHarness("/revoke-security"',
    "logoutRole(client, sessionId",
    "permissionRevocationPasswordCleared",
    "ownerDriftFocusNotRestored",
    "waitForRequestOwnersDrain(tracker, `${stage}.idle`)",
    "back.click()",
    "runtimeExceptions",
    "inspectPngPixels",
    "process.exit(await main())",
  ]) assert.ok(source.includes(required), required);
  const runStart = source.indexOf("async function run(runId)");
  const runSource = source.slice(runStart, source.indexOf("async function main()", runStart));
  const installClock = runSource.indexOf("await installBrowserClockGate(");
  const load = runSource.indexOf("await loadConsole(");
  const restoreClock = runSource.indexOf("await restoreBrowserClockGate(");
  assert.ok(runStart >= 0 && installClock >= 0 && load > installClock && restoreClock > load);
  assert.doesNotMatch(
    source,
    /localStorage\.setItem|sessionStorage\.setItem|console\.(?:log|error|warn)\s*\(/,
  );
});
