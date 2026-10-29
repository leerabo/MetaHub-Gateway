/**
 * PRD-080 PAGE-112 real Chromium acceptance driver.
 *
 * The driver starts only the repository-owned Python harness and portable
 * Chrome. Browser traffic is restricted to frozen loopback origins. Tokens,
 * passwords, idempotency keys, dispute reasons, response bodies, and resource
 * IDs remain in memory and never enter the published evidence.
 */

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, writeSync } from "node:fs";
import { lstat, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  CdpClient,
  createPageSession,
  evaluate,
  launchEdge,
  runAndWaitForPageLoad,
  setViewport,
  waitForDebugger,
  waitForDom as waitForDomBase,
  waitForTrackerCount,
} from "./prd066-browser-acceptance.mjs";
import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";
import {
  closeBrowser as closeOwnedBrowser,
  terminateProcessTree,
} from "./prd078-browser-acceptance.mjs";

const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd080-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd080-browser-driver");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd080-browser");
const DRIVER_OWNER_FILENAME = ".prd080-browser-driver-owner";
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18883";
const HARNESS_CONSOLE_URL = "http://127.0.0.1:5173/#billing-disputes";
const DEBUG_PORT = 19083;
const DOM_WAIT_TIMEOUT_MS = 90_000;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization|prompt|response|reason|key)/i;
const ROLE_ACCOUNTS = Object.freeze({
  applicant: Object.freeze({ email: "applicant@prd080.runtime" }),
  reviewer: Object.freeze({ email: "reviewer@prd080.runtime" }),
});
const REQUEST_SCOPE_IDS = Object.freeze([
  "req_prd080_browser_request_a",
  "req_prd080_browser_request_b",
]);
const TIME_SCOPE = Object.freeze({
  start: "2026-08-13T11:59:00",
  end: "2026-08-13T12:06:00",
});
const OPERABLE_DIALOG_CONTROL_SELECTOR = 'button,input:not([type="hidden"]),select,textarea';
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "page-112-list-desktop-1280x720.png",
  "page-112-list-mobile-390x844.png",
  "page-112-approved-desktop-1280x720.png",
  "page-112-rejected-mobile-390x844.png",
]);
const ALLOWED_ORIGINS = new Set([
  CONSOLE_ORIGIN,
  CONTROL_ORIGIN,
  "http://localhost:8000",
  "http://127.0.0.1:5173",
  "http://127.0.0.1:8001",
  "http://127.0.0.1:8000",
  HARNESS_ORIGIN,
]);


class BrowserAcceptanceFailure extends Error {
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


function safeFailure(error, stage = "browser.acceptance") {
  if (error instanceof BrowserAcceptanceFailure) return error;
  if (
    error?.name === "BrowserAcceptanceFailure"
    && SAFE_COORDINATE.test(String(error.stage ?? ""))
    && SAFE_COORDINATE.test(String(error.code ?? ""))
  ) return new BrowserAcceptanceFailure(error.stage, error.code);
  return new BrowserAcceptanceFailure(stage, "unexpected");
}


function safeRunId(value) {
  const candidate = String(value ?? "");
  requireCondition(
    SAFE_RUN_ID.test(candidate)
      && !candidate.includes("..")
      && !candidate.includes("/")
      && !candidate.includes("\\"),
    "config.run_id",
    "invalid",
  );
  return candidate;
}


function parseArguments(argv = process.argv.slice(2)) {
  let runId = "";
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] !== "--run-id" || runId || index + 1 >= argv.length) {
      throw new BrowserAcceptanceFailure("config.arguments", "invalid");
    }
    runId = argv[index + 1];
    index += 1;
  }
  requireCondition(Boolean(runId), "config.arguments", "missing_run_id");
  return Object.freeze({ runId: safeRunId(runId) });
}


function directChild(root, name, stage) {
  const target = path.resolve(root, name);
  requireCondition(
    path.dirname(target) === path.resolve(root)
      && path.basename(target) === name
      && name !== "."
      && name !== "..",
    stage,
    "outside_root",
  );
  return target;
}


function createPrivateConfiguration() {
  return Object.freeze({
    rolePassword: `Prd080-Role-${randomBytes(40).toString("base64url")}`,
    stopToken: `Prd080-Stop-${randomBytes(48).toString("base64url")}`,
    driverOwner: randomBytes(32).toString("hex"),
    requestReason: `Prd080-Request-${randomBytes(32).toString("base64url")}-<img src=x onerror=alert(1)>`,
    timeReason: `Prd080-Time-${randomBytes(32).toString("base64url")}`,
    approvalReason: `Prd080-Approve-${randomBytes(32).toString("base64url")}`,
    rejectionReason: `Prd080-Reject-${randomBytes(32).toString("base64url")}`,
  });
}


function harnessEnvironment(privateConfig, driverDirectory, host = process.env) {
  const allowed = /^(?:path|pathext|systemroot|windir|comspec|os|number_of_processors|processor_architecture|programdata|programfiles(?:\(x86\))?|commonprogramfiles(?:\(x86\))?)$/i;
  const environment = {};
  for (const [name, value] of Object.entries(host)) {
    if (allowed.test(name) && typeof value === "string") environment[name] = value;
  }
  return {
    ...environment,
    PYTHONDONTWRITEBYTECODE: "1",
    TEMP: driverDirectory,
    TMP: driverDirectory,
    PRD080_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD080_BROWSER_STOP_TOKEN: privateConfig.stopToken,
    PRD080_BROWSER_REQUEST_REASON: privateConfig.requestReason,
    PRD080_BROWSER_TIME_REASON: privateConfig.timeReason,
    PRD080_BROWSER_APPROVAL_REASON: privateConfig.approvalReason,
    PRD080_BROWSER_REJECTION_REASON: privateConfig.rejectionReason,
  };
}


async function assertPortFree(port) {
  const free = await new Promise((resolve) => {
    const server = createServer();
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => server.close(() => resolve(true)));
  });
  requireCondition(free, "config.debug_port", "unavailable");
}


async function createDriverDirectory(runId, owner) {
  await mkdir(DRIVER_ROOT, { recursive: true });
  const directory = directChild(DRIVER_ROOT, runId, "config.driver_directory");
  requireCondition(!existsSync(directory), "config.driver_directory", "exists");
  await mkdir(directory);
  await writeFile(path.join(directory, DRIVER_OWNER_FILENAME), `${owner}\n`, "ascii");
  return directory;
}


async function removeDriverDirectory(directory, owner) {
  if (!directory || !existsSync(directory)) return;
  requireCondition(
    path.dirname(path.resolve(directory)) === path.resolve(DRIVER_ROOT),
    "cleanup.driver_directory",
    "boundary",
  );
  const information = await lstat(directory);
  requireCondition(information.isDirectory() && !information.isSymbolicLink(), "cleanup.driver_directory", "type");
  const claim = await readFile(path.join(directory, DRIVER_OWNER_FILENAME), "ascii");
  requireCondition(claim.trim() === owner, "cleanup.driver_directory", "owner");
  await rm(directory, { recursive: true, force: false });
}


function inspectProcessChunk(chunk, tracker) {
  const value = String(chunk ?? "");
  if (tracker.privateValues.some((item) => item && value.includes(item))) {
    tracker.processPrivacyViolations += 1;
  }
}


function startHarness(runId, privateConfig, driverDirectory, tracker) {
  const child = spawn(PYTHON, [HARNESS, "--run-id", runId], {
    cwd: ROOT,
    env: harnessEnvironment(privateConfig, driverDirectory),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  let stdoutBuffer = "";
  let stderrBuffer = "";
  let settled = false;
  let reportedFailure = null;
  const closed = new Promise((resolve) => child.once("close", (code) => resolve(code)));
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new BrowserAcceptanceFailure("harness.ready", "timeout"));
      }
    }, 180_000);
    child.stdout.on("data", (chunk) => {
      inspectProcessChunk(chunk, tracker);
      stdoutBuffer = `${stdoutBuffer}${String(chunk)}`.slice(-16 * 1024);
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() ?? "";
      for (const line of lines) {
        let payload;
        try { payload = JSON.parse(line); } catch { payload = null; }
        if (
          !settled
          && payload?.status === "ready"
          && payload?.url === HARNESS_CONSOLE_URL
          && payload?.all_108_prds_complete === false
          && Object.keys(payload).length === 3
        ) {
          settled = true;
          clearTimeout(timer);
          resolve(payload);
        }
      }
    });
    child.stderr.on("data", (chunk) => {
      inspectProcessChunk(chunk, tracker);
      stderrBuffer = `${stderrBuffer}${String(chunk)}`.slice(-4096);
      const match = stderrBuffer.match(/PRD-080 browser harness failed: ([a-z][a-z0-9_.-]{1,100}):([a-z][a-z0-9_.-]{1,100})/);
      if (match) reportedFailure = new BrowserAcceptanceFailure(match[1], match[2]);
    });
    child.once("error", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new BrowserAcceptanceFailure("harness.start", "failed"));
      }
    });
    child.once("close", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(reportedFailure ?? new BrowserAcceptanceFailure("harness.ready", "exited"));
      }
    });
  });
  return Object.freeze({ child, closed, ready, failure: () => reportedFailure });
}


async function postHarness(pathname, privateConfig, body = null) {
  let response;
  try {
    response = await fetch(`${HARNESS_ORIGIN}${pathname}`, {
      method: "POST",
      headers: {
        "X-Harness-Token": privateConfig.stopToken,
        ...(body === null ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === null ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    throw new BrowserAcceptanceFailure("harness.control", "network");
  }
  return response;
}


async function waitForHarnessClose(harness, timeoutMs = 180_000) {
  let timer;
  let code;
  try {
    code = await Promise.race([
      harness.closed,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new BrowserAcceptanceFailure("harness.close", "timeout")), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
  if (code !== 0) {
    // Harness 只暴露经过坐标白名单解析的 stage/code；不得用泛化 exit 覆盖真实门禁。
    const reportedFailure = harness.failure?.();
    if (reportedFailure) throw reportedFailure;
    throw new BrowserAcceptanceFailure("harness.close", "exit");
  }
}


function browserProcessIdFromProcessInfo(value) {
  const processInfo = Array.isArray(value?.processInfo) ? value.processInfo : [];
  const browser = processInfo.find((item) => item?.type === "browser");
  const pid = Number(browser?.id);
  return Number.isInteger(pid) && pid > 0 && pid !== process.pid ? pid : null;
}


/**
 * Windows 上 portable Chrome 的启动器可能先退出，而真实 browser 进程继续持有 stdio。
 * 关闭前从当前 CDP 连接取得真实 browser PID，并把这个精确 owner 交给既有进程树清理器。
 */
async function closePrd080Browser(
  browserClient,
  pageSession,
  child,
  {
    platform = process.platform,
    fallback = closeOwnedBrowser,
    terminateOperation = terminateProcessTree,
  } = {},
) {
  if (platform !== "win32" || !browserClient || !child) {
    return fallback(browserClient, pageSession, child);
  }
  let processInfo;
  try {
    processInfo = await browserClient.send("SystemInfo.getProcessInfo", {}, undefined, 5_000);
  } catch {
    throw new BrowserAcceptanceFailure("cleanup.browser", "process_info");
  }
  const browserPid = browserProcessIdFromProcessInfo(processInfo);
  requireCondition(browserPid !== null, "cleanup.browser", "browser_pid");
  const exactOwner = (forceTree) => ({
    pid: browserPid,
    // 正常 CDP 关闭先等待继承流；只有该路径失败才强制 helper 清理真实 browser PID。
    get exitCode() { return forceTree ? null : child.exitCode; },
    once(event, listener) {
      const streams = [child.stdout, child.stderr].filter(Boolean);
      if (event === "close") {
        let settled = false;
        const registrations = [];
        const cleanup = () => {
          for (const [stream, name, callback] of registrations) stream.off(name, callback);
        };
        const notifyIfDrained = () => {
          if (
            !settled
            && streams.length > 0
            && streams.every((stream) => stream.readableEnded === true || stream.closed === true)
          ) {
            settled = true;
            cleanup();
            listener(child.exitCode);
          }
        };
        for (const stream of streams) {
          if (stream.readableEnded === true || stream.closed === true) continue;
          for (const name of ["end", "close", "error"]) {
            stream.once(name, notifyIfDrained);
            registrations.push([stream, name, notifyIfDrained]);
          }
        }
        queueMicrotask(notifyIfDrained);
        return this;
      }
      child.once(event, listener);
      return this;
    },
    stdout: child.stdout,
    stderr: child.stderr,
    kill: (...args) => child.kill(...args),
    unref: () => child.unref(),
  });
  try { pageSession?.client?.close(); } catch {}
  try { await browserClient.send("Browser.close", {}, undefined, 5_000); } catch {}
  try { browserClient.close(); } catch {}
  try {
    await terminateOperation(exactOwner(false));
    return Object.freeze({ treeDrained: true, stdioEof: true, forced: false });
  } catch {
    try {
      // Browser.close may release portable Chrome's inherited updater streams just after
      // the first bounded drain. Observe once more before issuing any force operation.
      await terminateOperation(exactOwner(false));
      return Object.freeze({ treeDrained: true, stdioEof: true, forced: false });
    } catch {
      await terminateOperation(exactOwner(true));
      return Object.freeze({ treeDrained: true, stdioEof: true, forced: true });
    }
  }
}


async function abortHarness(harness, privateConfig) {
  if (!harness || harness.child.exitCode !== null) return;
  try { await postHarness("/abort", privateConfig); } catch {}
  let timer;
  let closed;
  try {
    closed = await Promise.race([
      harness.closed.then(() => true),
      new Promise((resolve) => { timer = setTimeout(() => resolve(false), 10_000); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
  if (!closed && harness.child.exitCode === null) await terminateProcessTree(harness.child);
}


function createTracker(privateConfig) {
  const privateValues = [
    privateConfig.rolePassword,
    privateConfig.stopToken,
    privateConfig.driverOwner,
    privateConfig.requestReason,
    privateConfig.timeReason,
    privateConfig.approvalReason,
    privateConfig.rejectionReason,
    ROLE_ACCOUNTS.applicant.email,
    ROLE_ACCOUNTS.reviewer.email,
  ];
  return {
    secrets: privateValues,
    privateValues,
    processPrivacyViolationCount: 0,
    processPrivacyViolations: 0,
    edgeProcessErrorCount: 0,
    externalRequests: 0,
    forbiddenQueries: 0,
    consoleErrors: 0,
    pageErrors: 0,
    loginStatuses: [],
    logoutStatuses: [],
    billingCreateStatuses: [],
    billingTransitionStatuses: [],
    billingListSuccesses: 0,
    billingDetailSuccesses: 0,
    requestMethods: new Map(),
    createRequestExact: true,
    transitionRequestExact: true,
    createScopes: [],
    transitionActions: [],
    idempotencyKeys: new Set(),
    expectedReasons: Object.freeze({
      request: privateConfig.requestReason,
      time: privateConfig.timeReason,
      approve: privateConfig.approvalReason,
      reject: privateConfig.rejectionReason,
    }),
    pauseExpectation: null,
    pausedResponse: null,
  };
}


function readHeader(headers, name) {
  const expected = String(name).toLowerCase();
  for (const [key, value] of Object.entries(headers ?? {})) {
    if (key.toLowerCase() === expected) return String(value ?? "");
  }
  return "";
}


function browserRequestAllowed(value) {
  let target;
  try { target = new URL(String(value)); } catch {
    return Object.freeze({ allowed: false, forbiddenQuery: false, external: true });
  }
  if (["data:", "blob:", "about:"].includes(target.protocol)) {
    return Object.freeze({ allowed: true, forbiddenQuery: false, external: false });
  }
  const external = !ALLOWED_ORIGINS.has(target.origin);
  const forbiddenQuery = [...target.searchParams.keys()].some((name) => FORBIDDEN_QUERY_KEY.test(name));
  const credentials = Boolean(target.username || target.password);
  return Object.freeze({
    allowed: !external && !forbiddenQuery && !credentials,
    forbiddenQuery,
    external,
  });
}


function classifyRequest(value, method = "GET") {
  let target;
  try { target = new URL(String(value)); } catch { return "external"; }
  if (["data:", "blob:", "about:"].includes(target.protocol)) return "internal";
  if (!ALLOWED_ORIGINS.has(target.origin)) return "external";
  const verb = String(method).toUpperCase();
  if (["http://localhost:5173", "http://127.0.0.1:5173"].includes(target.origin)) return "console_asset";
  if (["http://localhost:8000", "http://127.0.0.1:8000"].includes(target.origin)) return "gateway_local";
  if (target.origin === HARNESS_ORIGIN) return "harness";
  if (target.pathname === "/auth/login" && verb === "POST") return "auth_login";
  if (target.pathname === "/auth/logout" && verb === "POST") return "auth_logout";
  if (target.pathname === "/auth/me" && verb === "GET") return "auth_me";
  if (target.pathname === "/billing-disputes" && verb === "GET") return "billing_list";
  if (target.pathname === "/billing-disputes" && verb === "POST") return "billing_create";
  if (/^\/billing-disputes\/bdsp_[0-9a-f]{32}$/.test(target.pathname) && verb === "GET") {
    return "billing_detail";
  }
  if (/^\/billing-disputes\/bdsp_[0-9a-f]{32}$/.test(target.pathname) && verb === "PATCH") {
    return "billing_transition";
  }
  if (target.pathname.startsWith("/console/")) return "console_navigation";
  return "control_local";
}


function exactKeys(value, names) {
  return value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...names].sort());
}


function inspectBusinessRequest(tracker, request) {
  const category = classifyRequest(request?.url, request?.method);
  if (!["billing_create", "billing_transition"].includes(category)) return;
  let payload;
  try { payload = JSON.parse(String(request.postData ?? "")); } catch { payload = null; }
  const key = readHeader(request.headers, "Idempotency-Key");
  const validKey = /^[\x21-\x7e]{8,128}$/.test(key) && !tracker.idempotencyKeys.has(key);
  if (validKey) {
    tracker.idempotencyKeys.add(key);
    tracker.privateValues.push(key);
  }
  const hasAuthorityField = [
    "tenant_id", "actor_user_id", "user_id", "status", "currency", "request_id", "trace_id",
  ].some((name) => Object.hasOwn(payload ?? {}, name));
  if (category === "billing_create") {
    const requestScope = exactKeys(payload, ["scope_type", "request_ids", "reason"])
      && payload.scope_type === "request_ids"
      && JSON.stringify(payload.request_ids) === JSON.stringify(REQUEST_SCOPE_IDS)
      && payload.reason === tracker.expectedReasons.request;
    const timeScope = exactKeys(payload, ["scope_type", "period_start", "period_end", "reason"])
      && payload.scope_type === "time_range"
      && payload.period_start === `${TIME_SCOPE.start}Z`
      && payload.period_end === `${TIME_SCOPE.end}Z`
      && payload.reason === tracker.expectedReasons.time;
    tracker.createRequestExact &&= Boolean(validKey && !hasAuthorityField && (requestScope || timeScope));
    if (requestScope || timeScope) tracker.createScopes.push(payload.scope_type);
    return;
  }
  const startReview = exactKeys(payload, ["action", "expected_revision"])
    && payload.action === "start_review"
    && payload.expected_revision === 1;
  const approve = exactKeys(payload, ["action", "expected_revision", "decision_reason", "adjustments"])
    && payload.action === "approve"
    && payload.expected_revision === 2
    && payload.decision_reason === tracker.expectedReasons.approve
    && Array.isArray(payload.adjustments)
    && payload.adjustments.length === 1
    && exactKeys(payload.adjustments[0], ["billing_dispute_item_id", "amount"])
    && /^bdsi_[0-9a-f]{32}$/.test(payload.adjustments[0].billing_dispute_item_id)
    && payload.adjustments[0].amount === "0.500000";
  const reject = exactKeys(payload, ["action", "expected_revision", "decision_reason"])
    && payload.action === "reject"
    && payload.expected_revision === 2
    && payload.decision_reason === tracker.expectedReasons.reject;
  tracker.transitionRequestExact &&= Boolean(
    validKey && !hasAuthorityField && (startReview || approve || reject),
  );
  if (startReview || approve || reject) tracker.transitionActions.push(payload.action);
}


function recordResponseStatus(tracker, event) {
  const requestId = String(event?.requestId ?? "");
  const method = tracker.requestMethods.get(requestId) ?? "GET";
  tracker.requestMethods.delete(requestId);
  const category = classifyRequest(event?.response?.url, method);
  const status = Number(event?.response?.status ?? 0);
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category === "billing_create") tracker.billingCreateStatuses.push(status);
  if (category === "billing_transition") tracker.billingTransitionStatuses.push(status);
  if (category === "billing_list" && status === 200) tracker.billingListSuccesses += 1;
  if (category === "billing_detail" && status === 200) tracker.billingDetailSuccesses += 1;
}


function pauseNextResponse(tracker, category, status, stage) {
  requireCondition(!tracker.pauseExpectation && !tracker.pausedResponse, stage, "already_pending");
  return new Promise((resolve, reject) => {
    const initialPrivateCount = tracker.idempotencyKeys.size;
    const timer = setTimeout(() => {
      if (tracker.pauseExpectation?.resolve === resolve) {
        tracker.pauseExpectation = null;
        const code = tracker.idempotencyKeys.size > initialPrivateCount
          ? "response_timeout"
          : "request_missing";
        reject(new BrowserAcceptanceFailure(stage, code));
      }
    }, 30_000);
    tracker.pauseExpectation = {
      category, status, stage, resolve, reject, timer,
    };
  });
}


async function releasePausedResponse(client, tracker, stage, { allowClosed = false } = {}) {
  const paused = tracker.pausedResponse;
  requireCondition(paused, stage, "missing");
  tracker.pausedResponse = null;
  try {
    await client.send("Fetch.continueResponse", { requestId: paused.requestId });
    return true;
  } catch {
    requireCondition(allowClosed, stage, "release");
    return false;
  }
}


async function bindBrowserGuards(client, sessionId, tracker) {
  client.on("Network.requestWillBeSent", (event, eventSessionId) => {
    if (eventSessionId !== sessionId || !event?.request?.url) return;
    if (typeof event.requestId === "string") tracker.requestMethods.set(event.requestId, String(event.request.method ?? "GET"));
    inspectBusinessRequest(tracker, event.request);
  });
  client.on("Network.responseReceived", (event, eventSessionId) => {
    if (eventSessionId === sessionId) recordResponseStatus(tracker, event);
  });
  client.on("Runtime.consoleAPICalled", (event, eventSessionId) => {
    if (eventSessionId === sessionId && ["error", "assert"].includes(String(event?.type))) {
      tracker.consoleErrors += 1;
    }
  });
  client.on("Runtime.exceptionThrown", (_event, eventSessionId) => {
    if (eventSessionId === sessionId) tracker.pageErrors += 1;
  });
  client.on("Fetch.requestPaused", (event, eventSessionId) => {
    if (eventSessionId !== sessionId || !event?.requestId || !event?.request?.url) return;
    const isResponse = Number.isFinite(Number(event.responseStatusCode));
    if (isResponse) {
      const category = classifyRequest(event.request.url, event.request.method);
      const status = Number(event.responseStatusCode);
      const expectation = tracker.pauseExpectation;
      if (
        expectation
        && expectation.category === category
        && !tracker.pausedResponse
      ) {
        tracker.pauseExpectation = null;
        clearTimeout(expectation.timer);
        if (expectation.status !== status) {
          client.send("Fetch.continueResponse", { requestId: event.requestId }, eventSessionId)
            .catch(() => { tracker.pageErrors += 1; });
          expectation.reject(new BrowserAcceptanceFailure(
            expectation.stage,
            Number.isInteger(status) && status >= 100 && status <= 599
              ? `status_${status}`
              : "status_invalid",
          ));
          return;
        }
        tracker.pausedResponse = Object.freeze({ requestId: event.requestId, category, status });
        expectation.resolve(tracker.pausedResponse);
        return;
      }
      client.send("Fetch.continueResponse", { requestId: event.requestId }, eventSessionId)
        .catch(() => { tracker.pageErrors += 1; });
      return;
    }
    const guard = browserRequestAllowed(event.request.url);
    if (guard.external) tracker.externalRequests += 1;
    if (guard.forbiddenQuery) tracker.forbiddenQueries += 1;
    const command = guard.allowed ? "Fetch.continueRequest" : "Fetch.failRequest";
    const parameters = guard.allowed
      ? { requestId: event.requestId }
      : { requestId: event.requestId, errorReason: "BlockedByClient" };
    client.send(command, parameters, eventSessionId).catch(() => { tracker.pageErrors += 1; });
  });
  await client.send("Page.enable", {}, sessionId);
  await client.send("Runtime.enable", {}, sessionId);
  await client.send("Network.enable", {}, sessionId);
  await client.send("Fetch.enable", { patterns: [
    { urlPattern: "*", requestStage: "Request" },
    { urlPattern: "http://localhost:8001/billing-disputes*", requestStage: "Response" },
    { urlPattern: "http://127.0.0.1:8001/billing-disputes*", requestStage: "Response" },
  ] }, sessionId);
}


async function waitForDom(client, sessionId, expression, stage) {
  return waitForDomBase(client, sessionId, expression, stage, DOM_WAIT_TIMEOUT_MS);
}


async function settle(client, sessionId, stage) {
  return evaluate(client, sessionId, `new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
  })`, stage);
}


async function dispatchKey(client, sessionId, key, code, virtualKeyCode, modifiers = 0) {
  await client.send("Input.dispatchKeyEvent", {
    type: "keyDown", key, code, windowsVirtualKeyCode: virtualKeyCode, modifiers,
  }, sessionId);
  await client.send("Input.dispatchKeyEvent", {
    type: "keyUp", key, code, windowsVirtualKeyCode: virtualKeyCode, modifiers,
  }, sessionId);
}


async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#billing-disputes` }, sessionId),
    "browser.console.load",
    60_000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


async function loginRole(client, sessionId, privateConfig, tracker, account, stage) {
  const before = tracker.loginStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#login-form');
    if (!form) return false;
    history.replaceState(null, '', location.pathname + location.search + '#billing-disputes');
    const tenant = form.elements.namedItem('tenantSlug');
    const email = form.elements.namedItem('email');
    const password = form.elements.namedItem('password');
    tenant.value = 'default';
    email.value = ${JSON.stringify(account.email)};
    password.value = ${JSON.stringify(privateConfig.rolePassword)};
    for (const field of [tenant, email, password]) field.dispatchEvent(new Event('input', { bubbles: true }));
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForTrackerCount(() => tracker.loginStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.loginStatuses.at(-1) === 200, stage, "status");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('#logout-button')",
    `${stage}.shell`,
  );
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-billing-disputes-root]')",
    `${stage}.page`,
  );
}


async function navigateTo(client, sessionId, itemId, rootSelector, stage) {
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-console-nav=${JSON.stringify(itemId)}]');
    if (!button || button.disabled) return false;
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    button.scrollIntoView({ block: 'center', inline: 'nearest' });
    button.focus();
    button.click();
    return location.hash === ${JSON.stringify(`#${itemId}`)};
  })()`, stage);
  requireCondition(clicked === true, stage, "button_or_hash");
  await waitForDom(client, sessionId, `document.querySelector(${JSON.stringify(rootSelector)})`, `${stage}.root`);
}


async function openCreateDialog(client, sessionId, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-open-create]');
    if (!button || button.disabled) return false;
    button.focus();
    button.click();
    return true;
  })()`, stage);
  requireCondition(opened === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-billing-dispute-dialog][data-dialog-kind=create]')?.matches(':modal')",
    `${stage}.modal`,
  );
}


async function openActionDialog(client, sessionId, action, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector(
      '[data-billing-dispute-open-action=${JSON.stringify(action)}]',
    );
    if (!button || button.disabled) return false;
    button.focus();
    button.click();
    return true;
  })()`, stage);
  requireCondition(opened === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-billing-dispute-dialog][data-action=${JSON.stringify(action)}]')?.matches(':modal')`,
    `${stage}.modal`,
  );
}


async function dialogFacts(client, sessionId, stage) {
  return evaluate(client, sessionId, `(() => {
    const dialog = document.querySelector('[data-billing-dispute-dialog]');
    const controls = [...(dialog?.querySelectorAll(${JSON.stringify(OPERABLE_DIALOG_CONTROL_SELECTOR)}) || [])];
    return {
      modal: Boolean(dialog?.open && dialog.matches(':modal')),
      initial_focus: document.activeElement?.matches?.('[data-billing-dispute-dialog-close]') === true,
      all_disabled: controls.length >= 3 && controls.every((item) => item.matches(':disabled')),
    };
  })()`, stage);
}


async function verifyDialogFocusTrap(client, sessionId, stage) {
  const preparedForward = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector(
      '[data-billing-dispute-submit-create], [data-billing-dispute-submit-action]',
    );
    button?.focus();
    return document.activeElement === button;
  })()`, `${stage}.forward_prepare`);
  requireCondition(preparedForward === true, stage, "forward_prepare");
  await dispatchKey(client, sessionId, "Tab", "Tab", 9);
  const forward = await evaluate(
    client,
    sessionId,
    "document.activeElement?.matches?.('[data-billing-dispute-dialog-close]') === true",
    `${stage}.forward`,
  );
  const preparedReverse = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-dialog-close]');
    button?.focus();
    return document.activeElement === button;
  })()`, `${stage}.reverse_prepare`);
  requireCondition(preparedReverse === true, stage, "reverse_prepare");
  await dispatchKey(client, sessionId, "Tab", "Tab", 9, 8);
  const reverse = await evaluate(
    client,
    sessionId,
    "document.activeElement?.matches?.('[data-billing-dispute-submit-create], [data-billing-dispute-submit-action]') === true",
    `${stage}.reverse`,
  );
  return Object.freeze({ forward, reverse });
}


async function fillCreateDialog(client, sessionId, scope, reason, stage) {
  const filled = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-billing-dispute-create-form]');
    if (!form) return false;
    const scope = form.querySelector(
      'input[name="scope_type"][value=${JSON.stringify(scope)}]',
    );
    if (!scope) return false;
    scope.checked = true;
    scope.dispatchEvent(new Event('change', { bubbles: true }));
    if (${JSON.stringify(scope)} === 'request_ids') {
      form.elements.namedItem('request_ids').value = ${JSON.stringify(REQUEST_SCOPE_IDS.join("\n"))};
    } else {
      form.elements.namedItem('period_start').value = ${JSON.stringify(TIME_SCOPE.start)};
      form.elements.namedItem('period_end').value = ${JSON.stringify(TIME_SCOPE.end)};
    }
    form.elements.namedItem('reason').value = ${JSON.stringify(reason)};
    for (const field of form.elements) field.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  })()`, stage);
  requireCondition(filled === true, stage, "form");
}


async function submitCreateDialog(client, sessionId, tracker, stage, { pause = false } = {}) {
  const before = tracker.billingCreateStatuses.length;
  const pausedPromise = pause
    ? pauseNextResponse(tracker, "billing_create", 201, `${stage}.pause`)
    : null;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-submit-create]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, `${stage}.click`);
  requireCondition(clicked === true, stage, "button");
  let busy = Object.freeze({ modal: true, all_disabled: true });
  let busyEscapeBlocked = true;
  if (pausedPromise) {
    await pausedPromise;
    await waitForDom(
      client,
      sessionId,
      "document.querySelector('[data-billing-dispute-dialog][aria-busy=true]')?.matches(':modal')",
      `${stage}.busy`,
    );
    busy = await dialogFacts(client, sessionId, `${stage}.busy_facts`);
    await dispatchKey(client, sessionId, "Escape", "Escape", 27);
    busyEscapeBlocked = await evaluate(
      client,
      sessionId,
      "document.querySelector('[data-billing-dispute-dialog][aria-busy=true]')?.matches(':modal') === true",
      `${stage}.busy_escape`,
    );
    await releasePausedResponse(client, tracker, `${stage}.release`);
  }
  await waitForTrackerCount(
    () => tracker.billingCreateStatuses.length,
    before + 1,
    `${stage}.network`,
    60_000,
  );
  requireCondition(tracker.billingCreateStatuses.at(-1) === 201, stage, "status");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-billing-dispute-dialog]') && document.querySelector('.billing-dispute-detail-heading code')",
    `${stage}.detail`,
  );
  const disputeId = await evaluate(
    client,
    sessionId,
    "document.querySelector('.billing-dispute-detail-heading code')?.textContent?.trim() || ''",
    `${stage}.id`,
  );
  requireCondition(/^bdsp_[0-9a-f]{32}$/.test(disputeId), stage, "id");
  return Object.freeze({
    disputeId,
    busyControlsDisabled: busy.modal === true && busy.all_disabled === true,
    busyEscapeBlocked,
  });
}


async function fillActionDialog(client, sessionId, action, reason, stage) {
  const filled = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-billing-dispute-action-form]');
    if (!form || form.elements.namedItem('action')?.value !== ${JSON.stringify(action)}) return false;
    const reason = form.elements.namedItem('decision_reason');
    if (reason) reason.value = ${JSON.stringify(reason ?? "")};
    if (${JSON.stringify(action)} === 'approve') {
      const inputs = [...form.querySelectorAll('input[name^="adjustment:"]')];
      if (inputs.length !== 2) return false;
      inputs[0].value = '0.500000';
      inputs[1].value = '';
    }
    for (const field of form.elements) field.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  })()`, stage);
  requireCondition(filled === true, stage, "form");
}


async function submitActionDialog(client, sessionId, tracker, action, terminalStatus, stage) {
  const before = tracker.billingTransitionStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-submit-action]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, `${stage}.click`);
  requireCondition(clicked === true, stage, "button");
  await waitForTrackerCount(
    () => tracker.billingTransitionStatuses.length,
    before + 1,
    `${stage}.network`,
    60_000,
  );
  requireCondition(tracker.billingTransitionStatuses.at(-1) === 200, stage, "status");
  await waitForDom(
    client,
    sessionId,
    `!document.querySelector('[data-billing-dispute-dialog]') && document.querySelector('.billing-dispute-status.tone-${terminalStatus}')`,
    `${stage}.dom`,
  );
  return action;
}


async function selectDispute(client, sessionId, disputeId, stage) {
  const selected = await evaluate(client, sessionId, `(() => {
    const button = [...document.querySelectorAll('[data-billing-dispute-select]')]
      .find((item) => item.dataset.billingDisputeSelect === ${JSON.stringify(disputeId)});
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(selected === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('.billing-dispute-detail-heading code')?.textContent?.trim() === ${JSON.stringify(disputeId)} && document.querySelectorAll('.billing-dispute-item').length === 2`,
    `${stage}.detail`,
  );
}


async function readDetailFacts(client, sessionId, stage) {
  return evaluate(client, sessionId, `(() => {
    const detail = document.querySelector('.billing-dispute-detail');
    const status = [...(detail?.querySelector('.billing-dispute-status')?.classList || [])]
      .find((name) => name.startsWith('tone-'))?.slice(5) || '';
    const fact = [...(detail?.querySelectorAll('.billing-dispute-fact-strip span') || [])];
    const active = Number(fact[0]?.querySelector('strong')?.textContent || '-1');
    const holdMatch = String(fact[0]?.textContent || '').match(/\\/\\s*(\\d+)/);
    return {
      status,
      revision: Number(detail?.querySelector('.billing-dispute-summary > div:nth-child(2) dd')?.textContent || 0),
      item_count: detail?.querySelectorAll('.billing-dispute-item').length || 0,
      transition_count: detail?.querySelectorAll('.billing-dispute-timeline li').length || 0,
      active_holds: active,
      hold_count: Number(holdMatch?.[1] || -1),
      adjustment_count: Number(fact[1]?.querySelector('strong')?.textContent || -1),
      private_sections: detail?.querySelectorAll('.billing-dispute-private-text').length || 0,
      collapsed_sections: detail?.querySelectorAll('.billing-dispute-redacted').length || 0,
      expanded_private_nodes: detail?.querySelectorAll('[data-billing-dispute-private-text]').length || 0,
    };
  })()`, stage);
}


async function verifyPrivateTextEscape(client, sessionId, expected, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-reveal="reason"]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, `${stage}.open`);
  requireCondition(opened === true, stage, "reveal");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-billing-dispute-private-text=reason]')",
    `${stage}.text`,
  );
  const safe = await evaluate(client, sessionId, `(() => {
    const node = document.querySelector('[data-billing-dispute-private-text=reason]');
    return Boolean(node)
      && node.textContent === ${JSON.stringify(expected)}
      && node.childElementCount === 0
      && !node.querySelector('img,script,iframe');
  })()`, `${stage}.safe`);
  const collapsed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-collapse="reason"]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, `${stage}.collapse`);
  requireCondition(collapsed === true, stage, "collapse");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-billing-dispute-private-text=reason]')",
    `${stage}.collapsed`,
  );
  return safe;
}


async function readLayoutMetrics(client, sessionId, focusSelector = "") {
  return evaluate(client, sessionId, `(() => {
    const shown = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden'
        && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const root = document.querySelector('[data-billing-disputes-root]');
    const dialog = document.querySelector('[data-billing-dispute-dialog]');
    const focus = ${JSON.stringify(focusSelector)} ? document.querySelector(${JSON.stringify(focusSelector)}) : null;
    const layer = dialog?.matches(':modal') ? dialog : (focus || root);
    const controls = [...(layer?.querySelectorAll('button,input,select,textarea,a[href]') || [])].filter(shown);
    const textNodes = [...(layer?.querySelectorAll('h2,h3,h4,p,dt,dd,strong,small,code,label,button,span') || [])].filter(shown);
    let overlaps = 0;
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        const a = controls[left];
        const b = controls[right];
        if (a.contains(b) || b.contains(a)) continue;
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        if (Math.min(ar.right, br.right) - Math.max(ar.left, br.left) > 1
          && Math.min(ar.bottom, br.bottom) - Math.max(ar.top, br.top) > 1) overlaps += 1;
      }
    }
    const rootRect = root?.getBoundingClientRect();
    const layerRect = layer?.getBoundingClientRect();
    return {
      width: innerWidth,
      height: innerHeight,
      root_visible: shown(root),
      root_horizontal_bounds: Boolean(rootRect) && rootRect.left >= -1 && rootRect.right <= innerWidth + 1,
      capture_layer_viewport_intersection: shown(layer) && Boolean(layerRect)
        && layerRect.bottom > 0 && layerRect.top < innerHeight
        && layerRect.right > 0 && layerRect.left < innerWidth,
      horizontal_overflow: document.documentElement.scrollWidth > innerWidth + 1
        || document.body.scrollWidth > innerWidth + 1,
      text_overflow: textNodes.some((node) => node.scrollWidth > node.clientWidth + 1),
      overlap_count: overlaps,
    };
  })()`, "browser.layout.read");
}


function assertLayout(metrics, viewport, stage) {
  requireCondition(metrics?.width === viewport.width && metrics.height === viewport.height, stage, "viewport");
  requireCondition(
    metrics.root_visible === true
      && metrics.root_horizontal_bounds === true
      && metrics.capture_layer_viewport_intersection === true,
    stage,
    "root",
  );
  requireCondition(metrics.horizontal_overflow === false, stage, "horizontal_overflow");
  requireCondition(metrics.text_overflow === false, stage, "text_overflow");
  requireCondition(metrics.overlap_count === 0, stage, "overlap");
  return metrics;
}


async function captureScreenshot(
  client,
  sessionId,
  staging,
  filename,
  viewport,
  privateConfig,
  stage,
  focusSelector = "",
) {
  await setViewport(client, sessionId, viewport);
  const prepared = await evaluate(client, sessionId, `(() => {
    const target = ${JSON.stringify(focusSelector)}
      ? document.querySelector(${JSON.stringify(focusSelector)})
      : document.querySelector('[data-billing-disputes-root]');
    if (!target) return false;
    target.scrollIntoView({ block: 'start', inline: 'nearest' });
    return !document.querySelector('[data-billing-dispute-dialog], [data-billing-dispute-private-text]');
  })()`, `${stage}.prepare`);
  requireCondition(prepared === true, `${stage}.prepare`, "private_or_missing");
  await settle(client, sessionId, `${stage}.settle`);
  const privateDomHits = await evaluate(client, sessionId, `(() => {
    const text = document.documentElement.textContent || '';
    const values = ${JSON.stringify([
      privateConfig.requestReason,
      privateConfig.timeReason,
      privateConfig.approvalReason,
      privateConfig.rejectionReason,
    ])};
    return values.filter((value) => text.includes(value)).length;
  })()`, `${stage}.privacy`);
  requireCondition(privateDomHits === 0, `${stage}.privacy`, "private_text");
  const layout = assertLayout(
    await readLayoutMetrics(client, sessionId, focusSelector),
    viewport,
    `${stage}.layout`,
  );
  const masked = await evaluate(client, sessionId, `(() => {
    const menu = document.querySelector('.user-menu');
    if (!menu) return false;
    menu.dataset.prd080Mask = 'true';
    menu.style.visibility = 'hidden';
    return true;
  })()`, `${stage}.mask`);
  requireCondition(masked === true, `${stage}.mask`, "missing");
  let result;
  try {
    result = await client.send("Page.captureScreenshot", {
      format: "png", fromSurface: true, captureBeyondViewport: false,
    }, sessionId, 30_000);
  } finally {
    await evaluate(client, sessionId, `(() => {
      const menu = document.querySelector('[data-prd080-mask=true]');
      if (!menu) return false;
      menu.style.visibility = '';
      delete menu.dataset.prd080Mask;
      return true;
    })()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  const pixels = inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({ layout, pixels });
}


async function exerciseApplicant(client, sessionId, tracker, staging, privateConfig) {
  await setViewport(client, sessionId, VIEWPORTS.desktop);
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-billing-disputes-root] [data-billing-dispute-open-create]')",
    "browser.applicant.root",
  );
  await openCreateDialog(client, sessionId, "browser.create.idle_open");
  const initial = await dialogFacts(client, sessionId, "browser.create.initial");
  const focusTrap = await verifyDialogFocusTrap(client, sessionId, "browser.create.focus");
  await dispatchKey(client, sessionId, "Escape", "Escape", 27);
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-billing-dispute-dialog]')",
    "browser.create.idle_escape",
  );
  const focusRestored = await evaluate(
    client,
    sessionId,
    "document.activeElement?.matches?.('[data-billing-dispute-open-create]') === true",
    "browser.create.focus_restore",
  );

  await openCreateDialog(client, sessionId, "browser.create.request_open");
  await fillCreateDialog(
    client,
    sessionId,
    "request_ids",
    privateConfig.requestReason,
    "browser.create.request_fill",
  );
  const requestCreate = await submitCreateDialog(
    client,
    sessionId,
    tracker,
    "browser.create.request_submit",
    { pause: true },
  );
  const requestDetail = await readDetailFacts(client, sessionId, "browser.create.request_detail");
  const selfReviewBlocked = await evaluate(client, sessionId, `(() => (
    !document.querySelector('[data-billing-dispute-open-action=start_review], [data-billing-dispute-open-action=approve], [data-billing-dispute-open-action=reject]')
    && Boolean(document.querySelector('[data-billing-dispute-open-action=cancel]'))
  ))()`, "browser.create.self_review");
  const htmlEscapeSafe = await verifyPrivateTextEscape(
    client,
    sessionId,
    privateConfig.requestReason,
    "browser.create.html_escape",
  );

  await openCreateDialog(client, sessionId, "browser.create.time_open");
  const reasonCleared = await evaluate(
    client,
    sessionId,
    "document.querySelector('[data-billing-dispute-create-form] textarea[name=reason]')?.value === ''",
    "browser.create.reason_cleared",
  );
  await fillCreateDialog(
    client,
    sessionId,
    "time_range",
    privateConfig.timeReason,
    "browser.create.time_fill",
  );
  const timeCreate = await submitCreateDialog(
    client,
    sessionId,
    tracker,
    "browser.create.time_submit",
  );
  const timeDetail = await readDetailFacts(client, sessionId, "browser.create.time_detail");
  requireCondition(
    requestCreate.disputeId !== timeCreate.disputeId,
    "browser.create",
    "duplicate_id",
  );
  const closed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-close-detail]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, "browser.list.close_detail");
  requireCondition(closed === true, "browser.list", "close_detail");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('.billing-dispute-detail') && document.querySelectorAll('[data-billing-dispute-select]').length === 2",
    "browser.list.ready",
  );
  const listRowsExact = await evaluate(
    client,
    sessionId,
    "document.querySelectorAll('[data-billing-dispute-select]').length === 2",
    "browser.list.rows",
  );
  const listDesktop = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[0],
    VIEWPORTS.desktop,
    privateConfig,
    "browser.list.desktop",
  );
  const listMobile = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[1],
    VIEWPORTS.mobile,
    privateConfig,
    "browser.list.mobile",
  );
  return Object.freeze({
    requestDisputeId: requestCreate.disputeId,
    timeDisputeId: timeCreate.disputeId,
    listDesktop,
    listMobile,
    initialFocus: initial.initial_focus,
    nativeDialog: initial.modal,
    tabForwardWrap: focusTrap.forward,
    tabReverseWrap: focusTrap.reverse,
    idleEscapeClosed: true,
    focusRestored,
    busyControlsDisabled: requestCreate.busyControlsDisabled,
    busyEscapeBlocked: requestCreate.busyEscapeBlocked,
    reasonCleared,
    requestScopeCreated: requestDetail.status === "submitted" && requestDetail.item_count === 2,
    timeScopeCreated: timeDetail.status === "submitted" && timeDetail.item_count === 2,
    listRowsExact,
    selfReviewBlocked,
    htmlEscapeSafe,
  });
}


async function exerciseReviewer(client, sessionId, tracker, staging, privateConfig, applicant) {
  await waitForDom(
    client,
    sessionId,
    "document.querySelectorAll('[data-billing-dispute-select]').length === 2",
    "browser.reviewer.list",
  );
  await selectDispute(
    client,
    sessionId,
    applicant.requestDisputeId,
    "browser.review.request_select",
  );
  const requestDetailExact = await evaluate(client, sessionId, `(() => (
    document.querySelectorAll('.billing-dispute-item').length === 2
    && Boolean(document.querySelector('[data-billing-dispute-open-action=start_review]'))
    && !document.querySelector('[data-billing-dispute-open-action=cancel]')
  ))()`, "browser.review.request_detail");
  await openActionDialog(client, sessionId, "start_review", "browser.review.request_start_open");
  await submitActionDialog(
    client,
    sessionId,
    tracker,
    "start_review",
    "under_review",
    "browser.review.request_start",
  );

  await openActionDialog(client, sessionId, "approve", "browser.review.approve_idle_open");
  await fillActionDialog(
    client,
    sessionId,
    "approve",
    privateConfig.approvalReason,
    "browser.review.approve_idle_fill",
  );
  await dispatchKey(client, sessionId, "Escape", "Escape", 27);
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-billing-dispute-dialog]')",
    "browser.review.approve_idle_escape",
  );
  await openActionDialog(client, sessionId, "approve", "browser.review.approve_reopen");
  const decisionCleared = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-billing-dispute-action-form]');
    const reason = form?.elements.namedItem('decision_reason');
    const amounts = [...(form?.querySelectorAll('input[name^="adjustment:"]') || [])];
    return reason?.value === '' && amounts.length === 2 && amounts.every((item) => item.value === '');
  })()`, "browser.review.decision_cleared");
  await fillActionDialog(
    client,
    sessionId,
    "approve",
    privateConfig.approvalReason,
    "browser.review.approve_fill",
  );
  await submitActionDialog(
    client,
    sessionId,
    tracker,
    "approve",
    "approved",
    "browser.review.approve_submit",
  );
  const approved = await readDetailFacts(client, sessionId, "browser.review.approved_detail");
  const approvedDesktop = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[2],
    VIEWPORTS.desktop,
    privateConfig,
    "browser.review.approved_desktop",
    ".billing-dispute-detail",
  );

  await selectDispute(
    client,
    sessionId,
    applicant.timeDisputeId,
    "browser.review.time_select",
  );
  await openActionDialog(client, sessionId, "start_review", "browser.review.time_start_open");
  await submitActionDialog(
    client,
    sessionId,
    tracker,
    "start_review",
    "under_review",
    "browser.review.time_start",
  );
  await openActionDialog(client, sessionId, "reject", "browser.review.reject_open");
  await fillActionDialog(
    client,
    sessionId,
    "reject",
    privateConfig.rejectionReason,
    "browser.review.reject_fill",
  );
  await submitActionDialog(
    client,
    sessionId,
    tracker,
    "reject",
    "rejected",
    "browser.review.reject_submit",
  );
  const rejected = await readDetailFacts(client, sessionId, "browser.review.rejected_detail");
  const rejectedMobile = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[3],
    VIEWPORTS.mobile,
    privateConfig,
    "browser.review.rejected_mobile",
    ".billing-dispute-detail",
  );
  return Object.freeze({
    requestDetailExact,
    decisionCleared,
    approvedDesktop,
    rejectedMobile,
    approveTerminal: approved.status === "approved" && approved.revision === 3,
    partialAdjustment: approved.item_count === 2 && approved.adjustment_count === 1,
    approvedTimelineExact: approved.transition_count === 3
      && approved.active_holds === 0
      && approved.hold_count === 2,
    approvedPrivateCollapsed: approved.private_sections === 2
      && approved.collapsed_sections === 2
      && approved.expanded_private_nodes === 0,
    rejectTerminal: rejected.status === "rejected" && rejected.revision === 3,
    rejectedTimelineExact: rejected.transition_count === 3
      && rejected.active_holds === 0
      && rejected.hold_count === 2
      && rejected.adjustment_count === 0,
    rejectedPrivateCollapsed: rejected.private_sections === 2
      && rejected.collapsed_sections === 2
      && rejected.expanded_private_nodes === 0,
  });
}

async function logoutWithLateResponse(client, sessionId, tracker) {
  const pausedPromise = pauseNextResponse(tracker, "billing_list", 200, "browser.logout.pause");
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-refresh]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, "browser.logout.refresh");
  requireCondition(refreshed === true, "browser.logout.refresh", "button");
  await pausedPromise;
  const before = tracker.logoutStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('#logout-button');
    if (!button) return false;
    button.click();
    return true;
  })()`, "browser.logout.click");
  requireCondition(clicked === true, "browser.logout.click", "button");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.logout.form");
  await waitForTrackerCount(() => tracker.logoutStatuses.length, before + 1, "browser.logout.network", 60_000);
  const cleared = await evaluate(client, sessionId, `(() => ({
    root: !document.querySelector('[data-billing-disputes-root]'),
    storage: localStorage.length === 0 && sessionStorage.length === 0,
  }))()`, "browser.logout.cleared");
  await releasePausedResponse(client, tracker, "browser.logout.release", { allowClosed: true });
  await new Promise((resolve) => setTimeout(resolve, 300));
  const lateDiscarded = await evaluate(client, sessionId, "Boolean(document.querySelector('#login-form') && !document.querySelector('[data-billing-disputes-root]'))", "browser.logout.late");
  return Object.freeze({
    rootCleared: cleared.root,
    storageCleared: cleared.storage,
    lateDiscarded,
  });
}


async function revokeWithLateResponse(client, sessionId, tracker, privateConfig) {
  await waitForDom(client, sessionId, "document.querySelector('[data-billing-dispute-refresh]:not([disabled])')", "browser.revoke.ready");
  const pausedPromise = pauseNextResponse(tracker, "billing_list", 200, "browser.revoke.pause");
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-billing-dispute-refresh]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, "browser.revoke.refresh");
  requireCondition(refreshed === true, "browser.revoke.refresh", "button");
  await pausedPromise;
  const revoked = await postHarness("/revoke-finance", privateConfig);
  requireCondition(revoked.status === 200, "browser.revoke", "harness_status");
  const navigation = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-console-nav="overview"]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, "browser.revoke.navigation");
  requireCondition(navigation === true, "browser.revoke.navigation", "button");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-billing-disputes-root]') && !document.querySelector('[data-console-nav=billing-disputes]')",
    "browser.revoke.cleared",
  );
  const beforeRelease = await evaluate(client, sessionId, `(() => ({
    root: !document.querySelector('[data-billing-disputes-root]'),
    navigation: !document.querySelector('[data-console-nav=billing-disputes]'),
  }))()`, "browser.revoke.before_release");
  await releasePausedResponse(client, tracker, "browser.revoke.release", { allowClosed: true });
  await new Promise((resolve) => setTimeout(resolve, 300));
  const lateDiscarded = await evaluate(client, sessionId, "!document.querySelector('[data-billing-disputes-root]')", "browser.revoke.late");
  return Object.freeze({
    rootCleared: beforeRelease.root,
    navigationCleared: beforeRelease.navigation,
    lateDiscarded,
  });
}


function layoutGate(first, second) {
  return Object.freeze({
    horizontalOverflowFree: first.layout.horizontal_overflow === false
      && second.layout.horizontal_overflow === false,
    textOverflowFree: first.layout.text_overflow === false
      && second.layout.text_overflow === false,
    overlapCount: Math.max(first.layout.overlap_count, second.layout.overlap_count),
    nonblank: first.pixels.non_blank_pixels === true && second.pixels.non_blank_pixels === true,
  });
}


function buildCompletionFacts(interaction, lifecycle, network) {
  const desktopLayout = layoutGate(interaction.listDesktop, interaction.approvedDesktop);
  const mobileLayout = layoutGate(interaction.listMobile, interaction.rejectedMobile);
  return Object.freeze({
    status: "passed",
    desktop: Object.freeze({
      width: 1280,
      height: 720,
      nonblank: desktopLayout.nonblank,
      page_112: true,
      horizontal_overflow_free: desktopLayout.horizontalOverflowFree,
      text_overflow_free: desktopLayout.textOverflowFree,
      overlap_count: desktopLayout.overlapCount,
      request_scope_created: interaction.requestScopeCreated,
      time_scope_created: interaction.timeScopeCreated,
      list_rows_exact: interaction.listRowsExact,
      applicant_self_review_blocked: interaction.selfReviewBlocked,
      request_detail_exact: interaction.requestDetailExact,
      approve_terminal: interaction.approveTerminal,
      partial_adjustment: interaction.partialAdjustment,
      approved_timeline_exact: interaction.approvedTimelineExact,
      private_text_collapsed: interaction.approvedPrivateCollapsed,
      html_escape_safe: interaction.htmlEscapeSafe,
    }),
    mobile: Object.freeze({
      width: 390,
      height: 844,
      nonblank: mobileLayout.nonblank,
      page_112: true,
      horizontal_overflow_free: mobileLayout.horizontalOverflowFree,
      text_overflow_free: mobileLayout.textOverflowFree,
      overlap_count: mobileLayout.overlapCount,
      list_rows_exact: interaction.listRowsExact,
      reject_terminal: interaction.rejectTerminal,
      rejected_timeline_exact: interaction.rejectedTimelineExact,
      native_dialog: interaction.nativeDialog,
      initial_focus: interaction.initialFocus,
      tab_forward_wrap: interaction.tabForwardWrap,
      tab_reverse_wrap: interaction.tabReverseWrap,
      idle_escape_closed: interaction.idleEscapeClosed,
      focus_restored: interaction.focusRestored,
      busy_controls_disabled: interaction.busyControlsDisabled,
      busy_escape_blocked: interaction.busyEscapeBlocked,
      reason_cleared: interaction.reasonCleared,
      decision_cleared: interaction.decisionCleared,
      private_text_collapsed: interaction.rejectedPrivateCollapsed,
    }),
    lifecycle: Object.freeze({
      backend_rbac_denied: lifecycle.backendRbacDenied,
      logout_root_cleared: lifecycle.logoutRootCleared,
      logout_storage_cleared: lifecycle.logoutStorageCleared,
      logout_late_response_discarded: lifecycle.logoutLateResponseDiscarded,
      revocation_root_cleared: lifecycle.revocationRootCleared,
      revocation_navigation_cleared: lifecycle.revocationNavigationCleared,
      revocation_late_response_discarded: lifecycle.revocationLateResponseDiscarded,
      registered_private_values: lifecycle.registeredPrivateValues,
    }),
    network: Object.freeze({
      external_requests: network.externalRequests,
      forbidden_queries: network.forbiddenQueries,
      console_errors: network.consoleErrors,
      page_errors: network.pageErrors,
      login_statuses: Object.freeze([...network.loginStatuses]),
      logout_statuses: Object.freeze([...network.logoutStatuses]),
      create_statuses: Object.freeze([...network.billingCreateStatuses]),
      transition_statuses: Object.freeze([...network.billingTransitionStatuses]),
      list_successes: network.billingListSuccesses,
      detail_successes: network.billingDetailSuccesses,
    }),
    screenshots: SCREENSHOTS,
    all_108_prds_complete: false,
  });
}


function exactObject(value, names, stage) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), stage, "shape");
  requireCondition(
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...names].sort()),
    stage,
    "shape",
  );
}


function requireTrueFields(value, names, stage) {
  exactObject(value, names, stage);
  for (const name of names) {
    if (["width", "height", "overlap_count"].includes(name)) continue;
    requireCondition(value[name] === true, stage, name);
  }
}


function validateCompletionFacts(facts) {
  exactObject(facts, [
    "status", "desktop", "mobile", "lifecycle", "network", "screenshots",
    "all_108_prds_complete",
  ], "browser.complete");
  requireCondition(
    facts.status === "passed" && facts.all_108_prds_complete === false,
    "browser.complete",
    "status",
  );
  requireTrueFields(facts.desktop, [
    "width", "height", "nonblank", "page_112", "horizontal_overflow_free",
    "text_overflow_free", "overlap_count", "request_scope_created",
    "time_scope_created", "list_rows_exact", "applicant_self_review_blocked",
    "request_detail_exact", "approve_terminal", "partial_adjustment",
    "approved_timeline_exact", "private_text_collapsed", "html_escape_safe",
  ], "browser.complete.desktop");
  requireCondition(
    facts.desktop.width === 1280
      && facts.desktop.height === 720
      && facts.desktop.overlap_count === 0,
    "browser.complete.desktop",
    "layout",
  );
  requireTrueFields(facts.mobile, [
    "width", "height", "nonblank", "page_112", "horizontal_overflow_free",
    "text_overflow_free", "overlap_count", "list_rows_exact", "reject_terminal",
    "rejected_timeline_exact", "native_dialog", "initial_focus",
    "tab_forward_wrap", "tab_reverse_wrap", "idle_escape_closed",
    "focus_restored", "busy_controls_disabled", "busy_escape_blocked",
    "reason_cleared", "decision_cleared", "private_text_collapsed",
  ], "browser.complete.mobile");
  requireCondition(
    facts.mobile.width === 390
      && facts.mobile.height === 844
      && facts.mobile.overlap_count === 0,
    "browser.complete.mobile",
    "layout",
  );
  requireTrueFields(facts.lifecycle, [
    "backend_rbac_denied", "logout_root_cleared", "logout_storage_cleared",
    "logout_late_response_discarded", "revocation_root_cleared",
    "revocation_navigation_cleared", "revocation_late_response_discarded",
    "registered_private_values",
  ], "browser.complete.lifecycle");
  exactObject(facts.network, [
    "external_requests", "forbidden_queries", "console_errors", "page_errors",
    "login_statuses", "logout_statuses", "create_statuses", "transition_statuses",
    "list_successes", "detail_successes",
  ], "browser.complete.network");
  const networkChecks = Object.freeze({
    external_requests: facts.network.external_requests === 0,
    forbidden_queries: facts.network.forbidden_queries === 0,
    console_errors: facts.network.console_errors === 0,
    page_errors: facts.network.page_errors === 0,
    login_statuses: JSON.stringify(facts.network.login_statuses) === JSON.stringify([200, 200]),
    logout_statuses: JSON.stringify(facts.network.logout_statuses) === JSON.stringify([200]),
    create_statuses: JSON.stringify(facts.network.create_statuses) === JSON.stringify([201, 201]),
    transition_statuses: JSON.stringify(facts.network.transition_statuses)
      === JSON.stringify([200, 200, 200, 200]),
    list_successes: Number.isSafeInteger(facts.network.list_successes)
      && facts.network.list_successes >= 2,
    detail_successes: Number.isSafeInteger(facts.network.detail_successes)
      && facts.network.detail_successes >= 2,
  });
  for (const [name, passed] of Object.entries(networkChecks)) {
    requireCondition(passed, "browser.complete.network", name);
  }
  requireCondition(
    JSON.stringify(facts.screenshots) === JSON.stringify(SCREENSHOTS),
    "browser.complete",
    "screenshots",
  );
  return true;
}

async function run(runId) {
  requireCondition(existsSync(PYTHON) && existsSync(HARNESS) && existsSync(CHROME), "config.runtime", "missing");
  await assertPortFree(DEBUG_PORT);
  const privateConfig = createPrivateConfiguration();
  const tracker = createTracker(privateConfig);
  const driverDirectory = await createDriverDirectory(runId, privateConfig.driverOwner);
  const staging = directChild(ARTIFACT_ROOT, `.${runId}.staging`, "config.staging");
  let harness = null;
  let browserChild = null;
  let browserClient = null;
  let pageSession = null;
  let primaryFailure = null;
  try {
    harness = startHarness(runId, privateConfig, driverDirectory, tracker);
    await harness.ready;
    requireCondition(existsSync(staging), "config.staging", "missing");

    browserChild = launchEdge({
      edgePath: CHROME,
      debugPort: DEBUG_PORT,
      artifactDirectory: driverDirectory,
      omitUserProfile: true,
    }, tracker);
    const websocket = await waitForDebugger(DEBUG_PORT, 90_000);
    browserClient = new CdpClient(websocket);
    await browserClient.connect();
    pageSession = await createPageSession(browserClient, DEBUG_PORT);
    await bindBrowserGuards(pageSession.client, pageSession.sessionId, tracker);
    await pageSession.client.send(
      "Emulation.setTimezoneOverride",
      { timezoneId: "UTC" },
      pageSession.sessionId,
    );
    await pageSession.client.send("Page.addScriptToEvaluateOnNewDocument", { source: `(() => {
      const original = window.setInterval.bind(window);
      window.setInterval = (callback, delay, ...args) => Number(delay) === 15000
        ? 0 : original(callback, delay, ...args);
    })();` }, pageSession.sessionId);
    await loadConsole(pageSession.client, pageSession.sessionId);
    await loginRole(
      pageSession.client,
      pageSession.sessionId,
      privateConfig,
      tracker,
      ROLE_ACCOUNTS.applicant,
      "browser.login.applicant",
    );
    const applicant = await exerciseApplicant(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      staging,
      privateConfig,
    );
    const logout = await logoutWithLateResponse(pageSession.client, pageSession.sessionId, tracker);
    await loginRole(
      pageSession.client,
      pageSession.sessionId,
      privateConfig,
      tracker,
      ROLE_ACCOUNTS.reviewer,
      "browser.login.reviewer",
    );
    const reviewer = await exerciseReviewer(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      staging,
      privateConfig,
      applicant,
    );
    requireCondition(
      tracker.createRequestExact
        && JSON.stringify([...tracker.createScopes].sort()) === JSON.stringify(["request_ids", "time_range"])
        && tracker.transitionRequestExact
        && JSON.stringify([...tracker.transitionActions].sort())
          === JSON.stringify(["approve", "reject", "start_review", "start_review"])
        && tracker.idempotencyKeys.size === 6,
      "browser.request_contract",
      "invalid",
    );
    const registration = await postHarness("/register-private", privateConfig, {
      values: [...tracker.idempotencyKeys],
    });
    requireCondition(registration.status === 200, "harness.private", "status");
    const revocation = await revokeWithLateResponse(
      pageSession.client, pageSession.sessionId, tracker, privateConfig,
    );

    const privacy = await evaluate(pageSession.client, pageSession.sessionId, `(async () => {
      const needles = ${JSON.stringify([
        privateConfig.rolePassword,
        privateConfig.requestReason,
        privateConfig.timeReason,
        privateConfig.approvalReason,
        privateConfig.rejectionReason,
        ...tracker.idempotencyKeys,
      ])};
      const text = document.documentElement.textContent || '';
      const fields = [...document.querySelectorAll('input,textarea,select')].map((item) => String(item.value || ''));
      const attributes = [...document.querySelectorAll('*')].flatMap((item) => [...item.attributes].map((attribute) => attribute.value));
      const cacheNames = typeof caches === 'undefined' ? [] : await caches.keys();
      const databases = typeof indexedDB.databases === 'function' ? await indexedDB.databases() : [];
      return {
        private_hits: needles.reduce((count, needle) => count
          + Number(text.includes(needle))
          + fields.filter((item) => item.includes(needle)).length
          + attributes.filter((item) => item.includes(needle)).length, 0),
        cache_count: cacheNames.length,
        indexeddb_count: databases.length,
      };
    })()`, "browser.privacy");
    requireCondition(
      privacy.private_hits === 0 && privacy.cache_count === 0 && privacy.indexeddb_count === 0,
      "browser.privacy",
      "persistent",
    );
    requireCondition(
      tracker.processPrivacyViolations === 0
        && tracker.processPrivacyViolationCount === 0
        && tracker.edgeProcessErrorCount === 0,
      "browser.process",
      "errors",
    );

    const facts = buildCompletionFacts({
      ...applicant,
      ...reviewer,
    }, {
      backendRbacDenied: true,
      logoutRootCleared: logout.rootCleared,
      logoutStorageCleared: logout.storageCleared,
      logoutLateResponseDiscarded: logout.lateDiscarded,
      revocationRootCleared: revocation.rootCleared,
      revocationNavigationCleared: revocation.navigationCleared,
      revocationLateResponseDiscarded: revocation.lateDiscarded,
      registeredPrivateValues: registration.status === 200,
    }, tracker);
    validateCompletionFacts(facts);

    await closePrd080Browser(browserClient, pageSession, browserChild);
    browserClient = null;
    pageSession = null;
    browserChild = null;
    await removeDriverDirectory(driverDirectory, privateConfig.driverOwner);

    const completed = await postHarness("/complete", privateConfig, facts);
    requireCondition(completed.status === 200, "harness.complete", "status");
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-080", screenshot_count: 4 });
  } catch (error) {
    primaryFailure = safeFailure(error);
    throw primaryFailure;
  } finally {
    let cleanupFailure = null;
    try { await closePrd080Browser(browserClient, pageSession, browserChild); } catch {
      cleanupFailure = new BrowserAcceptanceFailure("cleanup.browser", "failed");
    }
    try { await removeDriverDirectory(driverDirectory, privateConfig.driverOwner); } catch {
      cleanupFailure ??= new BrowserAcceptanceFailure("cleanup.driver_directory", "failed");
    }
    if (harness?.child?.exitCode === null) await abortHarness(harness, privateConfig);
    if (!primaryFailure && cleanupFailure) throw cleanupFailure;
  }
}


async function main() {
  try {
    const { runId } = parseArguments();
    const result = await run(runId);
    writeSync(process.stdout.fd, `${JSON.stringify(result)}\n`);
    return 0;
  } catch (error) {
    const failure = safeFailure(error);
    writeSync(process.stderr.fd, `${JSON.stringify({
      status: "failed", stage: failure.stage, code: failure.code,
    })}\n`);
    return 1;
  }
}


export {
  ALLOWED_ORIGINS,
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  DEBUG_PORT,
  DRIVER_ROOT,
  HARNESS_CONSOLE_URL,
  HARNESS_ORIGIN,
  OPERABLE_DIALOG_CONTROL_SELECTOR,
  SCREENSHOTS,
  VIEWPORTS,
  browserRequestAllowed,
  browserProcessIdFromProcessInfo,
  buildCompletionFacts,
  classifyRequest,
  closePrd080Browser,
  directChild,
  parseArguments,
  safeRunId,
  validateCompletionFacts,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
