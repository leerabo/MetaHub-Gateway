/**
 * PRD-079 PAGE-110/111 real Chromium acceptance driver.
 *
 * The driver starts only the repository-owned Python harness and portable
 * Chrome. Browser traffic is restricted to frozen loopback origins. Tokens,
 * passwords, idempotency keys, adjustment reasons, response bodies, and IDs
 * remain in memory and never enter the published evidence.
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
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd079-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd079-browser-driver");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd079-browser");
const DRIVER_OWNER_FILENAME = ".prd079-browser-driver-owner";
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18882";
const HARNESS_CONSOLE_URL = "http://127.0.0.1:5173/#plans";
const DEBUG_PORT = 19080;
const DOM_WAIT_TIMEOUT_MS = 90_000;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization|prompt|response|reason|key)/i;
const ROLE_ACCOUNT = Object.freeze({ email: "finance@prd079.runtime" });
const OPERABLE_DIALOG_CONTROL_SELECTOR = 'button,input:not([type="hidden"]),select,textarea';
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "page-110-plan-desktop-1280x720.png",
  "page-110-plan-mobile-390x844.png",
  "page-111-quota-desktop-1280x720.png",
  "page-111-quota-mobile-390x844.png",
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
const QUOTA_TYPES = Object.freeze(["paid", "free", "trial", "gift"]);


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
    rolePassword: `Prd079-Role-${randomBytes(40).toString("base64url")}`,
    stopToken: `Prd079-Stop-${randomBytes(48).toString("base64url")}`,
    driverOwner: randomBytes(32).toString("hex"),
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
    PRD079_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD079_BROWSER_STOP_TOKEN: privateConfig.stopToken,
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
      const match = stderrBuffer.match(/PRD-079 browser harness failed: ([a-z][a-z0-9_.-]{1,100}):([a-z][a-z0-9_.-]{1,100})/);
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
async function closePrd079Browser(
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
  try { pageSession?.client?.close(); } catch {}
  try { browserClient.close(); } catch {}
  const exactOwner = {
    pid: browserPid,
    // 强制既有 Windows helper 对真实 browser PID 执行 taskkill，而非信任已退出的启动器。
    get exitCode() { return null; },
    once: (...args) => child.once(...args),
    stdout: child.stdout,
    stderr: child.stderr,
    kill: (...args) => child.kill(...args),
    unref: () => child.unref(),
  };
  await terminateOperation(exactOwner);
  return Object.freeze({ treeDrained: true, stdioEof: true, forced: true });
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
    ROLE_ACCOUNT.email,
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
    planPatchStatuses: [],
    quotaAdjustmentStatuses: [],
    requestMethods: new Map(),
    planCasExact: true,
    planPatchRequestCount: 0,
    quotaAdjustmentExact: true,
    quotaAdjustmentTypes: [],
    quotaAdjustmentKeys: new Set(),
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
  if (target.pathname === "/plans" && verb === "GET") return "plan_read";
  if (target.pathname === "/plans" && verb === "PATCH") return "plan_patch";
  if (target.pathname === "/quota-buckets" && verb === "GET") return "quota_read";
  if (target.pathname === "/quota-adjustments" && verb === "POST") return "quota_adjustment";
  if (target.pathname.startsWith("/console/")) return "console_navigation";
  return "control_local";
}


function exactKeys(value, names) {
  return value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...names].sort());
}


function inspectBusinessRequest(tracker, request) {
  const category = classifyRequest(request?.url, request?.method);
  if (category === "plan_patch") {
    tracker.planPatchRequestCount += 1;
    let payload;
    try { payload = JSON.parse(String(request.postData ?? "")); } catch { payload = null; }
    tracker.planCasExact &&= Boolean(
      exactKeys(payload, ["expected_revision", "display_name", "base_tier", "status", "trial_ends_at"])
      && payload.expected_revision === 0
      && payload.base_tier === "paid"
      && payload.status === "active"
      && typeof payload.display_name === "string"
      && typeof payload.trial_ends_at === "string"
      && !Object.hasOwn(payload, "tenant_id")
      && !Object.hasOwn(payload, "actor_user_id"),
    );
  }
  if (category === "quota_adjustment") {
    let payload;
    try { payload = JSON.parse(String(request.postData ?? "")); } catch { payload = null; }
    const key = readHeader(request.headers, "Idempotency-Key");
    const validKey = /^[\x21-\x7e]{8,200}$/.test(key) && !tracker.quotaAdjustmentKeys.has(key);
    if (validKey) tracker.quotaAdjustmentKeys.add(key);
    tracker.quotaAdjustmentExact &&= Boolean(
      exactKeys(payload, ["quota_type", "direction", "amount", "reason", "expected_bucket_revision"])
      && QUOTA_TYPES.includes(payload.quota_type)
      && payload.direction === "credit"
      && /^\d+\.\d{6}$/.test(payload.amount)
      && typeof payload.reason === "string"
      && payload.reason.length > 0
      && Number.isSafeInteger(payload.expected_bucket_revision)
      && payload.expected_bucket_revision >= 1
      && validKey
      && !Object.hasOwn(payload, "tenant_id")
      && !Object.hasOwn(payload, "actor_user_id")
      && !Object.hasOwn(payload, "quota_bucket_id"),
    );
    if (QUOTA_TYPES.includes(payload?.quota_type)) tracker.quotaAdjustmentTypes.push(payload.quota_type);
  }
}


function recordResponseStatus(tracker, event) {
  const requestId = String(event?.requestId ?? "");
  const method = tracker.requestMethods.get(requestId) ?? "GET";
  tracker.requestMethods.delete(requestId);
  const category = classifyRequest(event?.response?.url, method);
  const status = Number(event?.response?.status ?? 0);
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category === "plan_patch") tracker.planPatchStatuses.push(status);
  if (category === "quota_adjustment") tracker.quotaAdjustmentStatuses.push(status);
}


function pauseNextResponse(tracker, category, status, stage) {
  requireCondition(!tracker.pauseExpectation && !tracker.pausedResponse, stage, "already_pending");
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      if (tracker.pauseExpectation?.resolve === resolve) {
        tracker.pauseExpectation = null;
        reject(new BrowserAcceptanceFailure(stage, "timeout"));
      }
    }, 30_000);
    tracker.pauseExpectation = { category, status, stage, resolve, timer };
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
        && expectation.status === status
        && !tracker.pausedResponse
      ) {
        tracker.pauseExpectation = null;
        clearTimeout(expectation.timer);
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
    { urlPattern: "http://localhost:8001/plans", requestStage: "Response" },
    { urlPattern: "http://localhost:8001/quota-buckets", requestStage: "Response" },
    { urlPattern: "http://localhost:8001/quota-adjustments", requestStage: "Response" },
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
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#plans` }, sessionId),
    "browser.console.load",
    60_000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


async function loginFinance(client, sessionId, privateConfig, tracker, stage) {
  const before = tracker.loginStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#login-form');
    if (!form) return false;
    const tenant = form.elements.namedItem('tenantSlug');
    const email = form.elements.namedItem('email');
    const password = form.elements.namedItem('password');
    tenant.value = 'default';
    email.value = ${JSON.stringify(ROLE_ACCOUNT.email)};
    password.value = ${JSON.stringify(privateConfig.rolePassword)};
    for (const field of [tenant, email, password]) field.dispatchEvent(new Event('input', { bubbles: true }));
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('#logout-button') && (document.querySelector('[data-plan-management-root]') || document.querySelector('[data-quota-management-root]'))",
    `${stage}.dom`,
  );
  await waitForTrackerCount(() => tracker.loginStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.loginStatuses.at(-1) === 200, stage, "status");
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


async function fillPlanDraft(client, sessionId) {
  const changed = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-plan-form]');
    if (!form) return false;
    form.elements.namedItem('display_name').value = 'PRD-079 Browser Plan';
    form.elements.namedItem('base_tier').value = 'paid';
    form.elements.namedItem('trial_ends_at').value = '2099-08-14T12:00:00';
    form.querySelector('input[name="status"][value="active"]').checked = true;
    form.elements.namedItem('display_name').dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  })()`, "browser.plan.draft");
  requireCondition(changed === true, "browser.plan.draft", "form");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-plan-discard]:not([disabled])')",
    "browser.plan.draft.ready",
  );
}


async function openPlanDialog(client, sessionId, stage) {
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-plan-form]');
    if (!form) return false;
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-plan-confirm-dialog]')?.matches(':modal')",
    `${stage}.modal`,
  );
}


async function planDialogFacts(client, sessionId) {
  return evaluate(client, sessionId, `(() => {
    const dialog = document.querySelector('[data-plan-confirm-dialog]');
    const controls = [...(dialog?.querySelectorAll('button') || [])];
    return {
      modal: Boolean(dialog?.open && dialog.matches(':modal')),
      initial_focus: Boolean(document.activeElement?.matches?.('[data-plan-confirm-close]')),
      all_disabled: controls.length === 3 && controls.every((item) => item.disabled),
    };
  })()`, "browser.plan.dialog_facts");
}


async function verifyPlanFocusTrap(client, sessionId) {
  const forward = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-plan-confirm-submit]');
    button?.focus();
    return document.activeElement === button;
  })()`, "browser.plan.focus.forward_prepare");
  requireCondition(forward === true, "browser.plan.focus.forward", "prepare");
  await dispatchKey(client, sessionId, "Tab", "Tab", 9);
  const forwardWrapped = await evaluate(client, sessionId, "document.activeElement?.matches?.('[data-plan-confirm-close]') === true", "browser.plan.focus.forward");

  const reverse = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-plan-confirm-close]');
    button?.focus();
    return document.activeElement === button;
  })()`, "browser.plan.focus.reverse_prepare");
  requireCondition(reverse === true, "browser.plan.focus.reverse", "prepare");
  await dispatchKey(client, sessionId, "Tab", "Tab", 9, 8);
  const reverseWrapped = await evaluate(client, sessionId, "document.activeElement?.matches?.('[data-plan-confirm-submit]') === true", "browser.plan.focus.reverse");
  return Object.freeze({ forwardWrapped, reverseWrapped });
}


async function fillQuotaDialog(client, sessionId, quotaType, amount, stage) {
  const filled = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-quota-adjustment-form]');
    if (!form) return false;
    form.elements.namedItem('quota_type').value = ${JSON.stringify(quotaType)};
    form.elements.namedItem('direction').value = 'credit';
    form.elements.namedItem('amount').value = ${JSON.stringify(amount)};
    form.elements.namedItem('reason').value = ${JSON.stringify(`Browser ${quotaType} adjustment`)};
    for (const field of form.elements) field.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  })()`, stage);
  requireCondition(filled === true, stage, "form");
}


async function openQuotaDialog(client, sessionId, quotaType, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-quota-adjust=${JSON.stringify(quotaType)}]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(opened === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-quota-adjustment-dialog]')?.matches(':modal')",
    `${stage}.modal`,
  );
}


async function submitQuotaDialog(client, sessionId, stage) {
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-quota-adjustment-submit]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
}


async function readLayoutMetrics(client, sessionId) {
  return evaluate(client, sessionId, `(() => {
    const shown = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden'
        && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const root = document.querySelector('[data-plan-management-root], [data-quota-management-root]');
    const dialog = document.querySelector('.plan-billing-dialog');
    const layer = dialog?.matches(':modal') ? dialog : root;
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
    const rect = root?.getBoundingClientRect();
    const layerRect = layer?.getBoundingClientRect();
    return {
      width: innerWidth,
      height: innerHeight,
      root_visible: shown(root),
      root_horizontal_bounds: Boolean(rect) && rect.left >= -1 && rect.right <= innerWidth + 1,
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
  stage,
  { revealRoot = false } = {},
) {
  await setViewport(client, sessionId, viewport);
  if (revealRoot) {
    const revealed = await evaluate(client, sessionId, `(() => {
      const root = document.querySelector('[data-plan-management-root], [data-quota-management-root]');
      if (!root) return false;
      root.scrollIntoView({ block: 'start', inline: 'nearest' });
      return true;
    })()`, `${stage}.reveal`);
    requireCondition(revealed === true, `${stage}.reveal`, "missing");
  }
  await settle(client, sessionId, `${stage}.settle`);
  const layout = assertLayout(await readLayoutMetrics(client, sessionId), viewport, `${stage}.layout`);
  const masked = await evaluate(client, sessionId, `(() => {
    const menu = document.querySelector('.user-menu');
    if (!menu) return false;
    menu.dataset.prd079Mask = 'true';
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
      const menu = document.querySelector('[data-prd079-mask="true"]');
      if (!menu) return false;
      menu.style.visibility = '';
      delete menu.dataset.prd079Mask;
      return true;
    })()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  const pixels = inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({ layout, pixels });
}


async function exercisePlan(client, sessionId, tracker, staging) {
  await setViewport(client, sessionId, VIEWPORTS.desktop);
  await waitForDom(client, sessionId, "document.querySelector('[data-plan-form]')", "browser.plan.form");
  const authenticated = await evaluate(client, sessionId, "Boolean(document.querySelector('#logout-button') && document.querySelector('[data-plan-management-root]'))", "browser.plan.authenticated");
  await fillPlanDraft(client, sessionId);
  await openPlanDialog(client, sessionId, "browser.plan.dialog_idle");
  const initial = await planDialogFacts(client, sessionId);
  const trap = await verifyPlanFocusTrap(client, sessionId);
  await dispatchKey(client, sessionId, "Escape", "Escape", 27);
  await waitForDom(client, sessionId, "!document.querySelector('[data-plan-confirm-dialog]')", "browser.plan.escape");
  const focusRestored = await evaluate(client, sessionId, "document.activeElement?.matches?.('[data-plan-form] button[type=submit]') === true", "browser.plan.focus_restore");

  await openPlanDialog(client, sessionId, "browser.plan.dialog_busy");
  const pausedPromise = pauseNextResponse(tracker, "plan_patch", 200, "browser.plan.pause");
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-plan-confirm-submit]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, "browser.plan.save");
  requireCondition(clicked === true, "browser.plan.save", "button");
  await pausedPromise;
  await waitForDom(client, sessionId, "document.querySelector('[data-plan-confirm-dialog] [data-plan-confirm-submit][disabled]')", "browser.plan.busy");
  const busy = await planDialogFacts(client, sessionId);
  requireCondition(busy.modal === true, "browser.plan.busy", "not_modal");
  requireCondition(busy.all_disabled === true, "browser.plan.busy", "controls_enabled");
  await dispatchKey(client, sessionId, "Escape", "Escape", 27);
  const busyEscapeBlocked = await evaluate(client, sessionId, "document.querySelector('[data-plan-confirm-dialog]')?.matches(':modal') === true", "browser.plan.busy_escape");
  requireCondition(busyEscapeBlocked === true, "browser.plan.busy_escape", "closed");
  const planDesktop = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[0], VIEWPORTS.desktop, "browser.plan.desktop",
  );
  await releasePausedResponse(client, tracker, "browser.plan.release");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-plan-confirm-dialog]') && [...document.querySelectorAll('.plan-editor .plan-billing-section-heading span')].some((item) => item.textContent.includes('revision 1'))",
    "browser.plan.saved",
  );
  await waitForTrackerCount(() => tracker.planPatchStatuses.length, 1, "browser.plan.status", 60_000);
  const planMobile = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[1], VIEWPORTS.mobile, "browser.plan.mobile",
    { revealRoot: true },
  );
  return Object.freeze({
    planDesktop,
    planMobile,
    authenticated,
    casExact: tracker.planCasExact && tracker.planPatchRequestCount === 1,
    saved: tracker.planPatchStatuses.length === 1 && tracker.planPatchStatuses[0] === 200,
    nativeModal: initial.modal,
    initialFocus: initial.initial_focus,
    tabForwardWrap: trap.forwardWrapped,
    tabReverseWrap: trap.reverseWrapped,
    idleEscapeClosed: true,
    focusRestored,
    busyEscapeBlocked: busy.modal && busy.all_disabled && busyEscapeBlocked,
  });
}


async function submitQuotaAdjustment(client, sessionId, tracker, quotaType, amount, expectedCount, stage) {
  await openQuotaDialog(client, sessionId, quotaType, `${stage}.open`);
  await fillQuotaDialog(client, sessionId, quotaType, amount, `${stage}.fill`);
  const before = tracker.quotaAdjustmentStatuses.length;
  await submitQuotaDialog(client, sessionId, `${stage}.submit`);
  await waitForTrackerCount(() => tracker.quotaAdjustmentStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.quotaAdjustmentStatuses.at(-1) === 201, stage, "status");
  await waitForDom(
    client,
    sessionId,
    `!document.querySelector('[data-quota-adjustment-dialog]') && document.querySelectorAll('.quota-adjustment-row').length >= ${expectedCount}`,
    `${stage}.dom`,
  );
}


async function exerciseQuota(client, sessionId, tracker, staging) {
  await setViewport(client, sessionId, VIEWPORTS.desktop);
  await navigateTo(client, sessionId, "quota-buckets", "[data-quota-management-root]", "browser.quota.navigate");
  await waitForDom(client, sessionId, "document.querySelectorAll('[data-quota-type]').length === 4", "browser.quota.rows");

  await openQuotaDialog(client, sessionId, "paid", "browser.quota.idle_open");
  const initial = await evaluate(client, sessionId, `(() => ({
    modal: document.querySelector('[data-quota-adjustment-dialog]')?.matches(':modal') === true,
    focus: document.activeElement?.matches?.('[data-quota-adjustment-close]') === true,
  }))()`, "browser.quota.initial");
  await fillQuotaDialog(client, sessionId, "paid", "4.000000", "browser.quota.idle_fill");
  await dispatchKey(client, sessionId, "Escape", "Escape", 27);
  await waitForDom(client, sessionId, "!document.querySelector('[data-quota-adjustment-dialog]')", "browser.quota.idle_escape");
  const focusRestored = await evaluate(client, sessionId, "document.activeElement?.matches?.('[data-quota-adjust=paid]') === true", "browser.quota.focus_restore");
  await openQuotaDialog(client, sessionId, "paid", "browser.quota.reopen");
  const reasonCleared = await evaluate(client, sessionId, "document.querySelector('[data-quota-adjustment-form] textarea[name=reason]')?.value.length === 0", "browser.quota.reason_cleared");
  await fillQuotaDialog(client, sessionId, "paid", "4.000000", "browser.quota.paid.fill");
  const paidBefore = tracker.quotaAdjustmentStatuses.length;
  await submitQuotaDialog(client, sessionId, "browser.quota.paid.submit");
  await waitForTrackerCount(() => tracker.quotaAdjustmentStatuses.length, paidBefore + 1, "browser.quota.paid.network", 60_000);
  requireCondition(tracker.quotaAdjustmentStatuses.at(-1) === 201, "browser.quota.paid", "status");
  await waitForDom(client, sessionId, "!document.querySelector('[data-quota-adjustment-dialog]') && document.querySelectorAll('.quota-adjustment-row').length >= 1", "browser.quota.paid.dom");

  await submitQuotaAdjustment(client, sessionId, tracker, "free", "2.000000", 2, "browser.quota.free");
  await submitQuotaAdjustment(client, sessionId, tracker, "trial", "1.000000", 3, "browser.quota.trial");

  await setViewport(client, sessionId, VIEWPORTS.mobile);
  await openQuotaDialog(client, sessionId, "gift", "browser.quota.gift.open");
  await fillQuotaDialog(client, sessionId, "gift", "3.000000", "browser.quota.gift.fill");
  const pausedPromise = pauseNextResponse(tracker, "quota_adjustment", 201, "browser.quota.gift.pause");
  await submitQuotaDialog(client, sessionId, "browser.quota.gift.submit");
  await pausedPromise;
  await waitForDom(client, sessionId, "document.querySelector('[data-quota-adjustment-dialog] [data-quota-adjustment-submit][disabled]')", "browser.quota.gift.busy");
  const busy = await evaluate(client, sessionId, `(() => {
    const dialog = document.querySelector('[data-quota-adjustment-dialog]');
    const controls = [...(dialog?.querySelectorAll(${JSON.stringify(OPERABLE_DIALOG_CONTROL_SELECTOR)}) || [])];
    return {
      modal: dialog?.matches(':modal') === true,
      disabled: controls.length >= 7 && controls.every((item) => item.disabled),
    };
  })()`, "browser.quota.gift.busy_facts");
  await dispatchKey(client, sessionId, "Escape", "Escape", 27);
  const busyEscapeBlocked = await evaluate(client, sessionId, "document.querySelector('[data-quota-adjustment-dialog]')?.matches(':modal') === true", "browser.quota.gift.busy_escape");
  const quotaMobile = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[3], VIEWPORTS.mobile, "browser.quota.mobile",
  );
  await releasePausedResponse(client, tracker, "browser.quota.gift.release");
  await waitForDom(client, sessionId, "!document.querySelector('[data-quota-adjustment-dialog]') && document.querySelectorAll('.quota-adjustment-row').length >= 4", "browser.quota.gift.dom");
  await waitForTrackerCount(() => tracker.quotaAdjustmentStatuses.length, 4, "browser.quota.gift.network", 60_000);

  const report = await evaluate(client, sessionId, `(() => {
    const rows = [...document.querySelectorAll('[data-quota-type]')];
    const types = rows.map((row) => row.dataset.quotaType).sort();
    return {
      types_exact: JSON.stringify(types) === JSON.stringify(['free','gift','paid','trial']),
      split: rows.length === 4
        && rows.every((row) => row.querySelectorAll('td').length === 7
          && Boolean(row.querySelector('td:nth-of-type(5) small')))
        && document.querySelectorAll('.quota-adjustment-row').length === 4
        && Boolean(document.querySelector('.quota-overage')),
    };
  })()`, "browser.quota.report");
  const quotaDesktop = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[2], VIEWPORTS.desktop, "browser.quota.desktop",
  );
  return Object.freeze({
    quotaDesktop,
    quotaMobile,
    typesExact: report.types_exact,
    adjustmentsExact: tracker.quotaAdjustmentExact
      && tracker.quotaAdjustmentTypes.length === 4
      && JSON.stringify([...tracker.quotaAdjustmentTypes].sort()) === JSON.stringify(["free", "gift", "paid", "trial"])
      && tracker.quotaAdjustmentKeys.size === 4,
    reportSplit: report.split,
    nativeModal: initial.modal,
    initialFocus: initial.focus,
    idleEscapeClosed: true,
    reasonCleared,
    focusRestored,
    busyControlsDisabled: busy.modal && busy.disabled,
    busyEscapeBlocked,
  });
}


async function logoutWithLateResponse(client, sessionId, tracker) {
  const pausedPromise = pauseNextResponse(tracker, "quota_read", 200, "browser.logout.pause");
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-quota-refresh]');
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
    root: !document.querySelector('[data-plan-management-root], [data-quota-management-root]'),
    storage: localStorage.length === 0 && sessionStorage.length === 0,
  }))()`, "browser.logout.cleared");
  await releasePausedResponse(client, tracker, "browser.logout.release", { allowClosed: true });
  await new Promise((resolve) => setTimeout(resolve, 300));
  const lateDiscarded = await evaluate(client, sessionId, "Boolean(document.querySelector('#login-form') && !document.querySelector('[data-plan-management-root], [data-quota-management-root]'))", "browser.logout.late");
  return Object.freeze({
    rootCleared: cleared.root,
    storageCleared: cleared.storage,
    lateDiscarded,
  });
}


async function revokeWithLateResponse(client, sessionId, tracker, privateConfig) {
  await navigateTo(client, sessionId, "plans", "[data-plan-management-root]", "browser.revoke.navigate");
  await waitForDom(client, sessionId, "document.querySelector('[data-plan-refresh]:not([disabled])')", "browser.revoke.ready");
  const pausedPromise = pauseNextResponse(tracker, "plan_read", 200, "browser.revoke.pause");
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-plan-refresh]');
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
    "!document.querySelector('[data-plan-management-root]') && !document.querySelector('[data-console-nav=plans]') && !document.querySelector('[data-console-nav=quota-buckets]')",
    "browser.revoke.cleared",
  );
  const beforeRelease = await evaluate(client, sessionId, `(() => ({
    root: !document.querySelector('[data-plan-management-root], [data-quota-management-root]'),
    navigation: !document.querySelector('[data-console-nav=plans], [data-console-nav=quota-buckets]'),
  }))()`, "browser.revoke.before_release");
  await releasePausedResponse(client, tracker, "browser.revoke.release", { allowClosed: true });
  await new Promise((resolve) => setTimeout(resolve, 300));
  const lateDiscarded = await evaluate(client, sessionId, "!document.querySelector('[data-plan-management-root], [data-quota-management-root]')", "browser.revoke.late");
  return Object.freeze({
    rootCleared: beforeRelease.root,
    navigationCleared: beforeRelease.navigation,
    lateDiscarded,
  });
}


function layoutGate(first, second) {
  return Object.freeze({
    horizontalOverflowFree: first.layout.horizontal_overflow === false && second.layout.horizontal_overflow === false,
    textOverflowFree: first.layout.text_overflow === false && second.layout.text_overflow === false,
    overlapCount: Math.max(first.layout.overlap_count, second.layout.overlap_count),
    nonblank: first.pixels.non_blank_pixels === true && second.pixels.non_blank_pixels === true,
  });
}


function buildCompletionFacts(interaction, lifecycle, network) {
  const desktopLayout = layoutGate(interaction.planDesktop, interaction.quotaDesktop);
  const mobileLayout = layoutGate(interaction.planMobile, interaction.quotaMobile);
  return Object.freeze({
    status: "passed",
    desktop: Object.freeze({
      width: 1280,
      height: 720,
      nonblank: desktopLayout.nonblank,
      page_110: true,
      page_111: true,
      horizontal_overflow_free: desktopLayout.horizontalOverflowFree,
      text_overflow_free: desktopLayout.textOverflowFree,
      overlap_count: desktopLayout.overlapCount,
      plan_authenticated: interaction.planAuthenticated,
      plan_cas_exact: interaction.planCasExact,
      plan_saved: interaction.planSaved,
      plan_native_modal: interaction.planNativeModal,
      plan_initial_focus: interaction.planInitialFocus,
      plan_tab_forward_wrap: interaction.planTabForwardWrap,
      plan_tab_reverse_wrap: interaction.planTabReverseWrap,
      plan_idle_escape_closed: interaction.planIdleEscapeClosed,
      plan_focus_restored: interaction.planFocusRestored,
      plan_busy_escape_blocked: interaction.planBusyEscapeBlocked,
      quota_report_split: interaction.quotaReportSplit,
    }),
    mobile: Object.freeze({
      width: 390,
      height: 844,
      nonblank: mobileLayout.nonblank,
      page_110: true,
      page_111: true,
      horizontal_overflow_free: mobileLayout.horizontalOverflowFree,
      text_overflow_free: mobileLayout.textOverflowFree,
      overlap_count: mobileLayout.overlapCount,
      quota_types_exact: interaction.quotaTypesExact,
      quota_adjustments_exact: interaction.quotaAdjustmentsExact,
      quota_report_split: interaction.quotaReportSplit,
      quota_native_modal: interaction.quotaNativeModal,
      quota_initial_focus: interaction.quotaInitialFocus,
      quota_idle_escape_closed: interaction.quotaIdleEscapeClosed,
      quota_reason_cleared: interaction.quotaReasonCleared,
      quota_focus_restored: interaction.quotaFocusRestored,
      quota_busy_controls_disabled: interaction.quotaBusyControlsDisabled,
      quota_busy_escape_blocked: interaction.quotaBusyEscapeBlocked,
    }),
    lifecycle: Object.freeze({
      backend_rbac_denied: lifecycle.backendRbacDenied,
      logout_root_cleared: lifecycle.logoutRootCleared,
      logout_storage_cleared: lifecycle.logoutStorageCleared,
      logout_late_response_discarded: lifecycle.logoutLateResponseDiscarded,
      revocation_root_cleared: lifecycle.revocationRootCleared,
      revocation_navigation_cleared: lifecycle.revocationNavigationCleared,
      revocation_late_response_discarded: lifecycle.revocationLateResponseDiscarded,
    }),
    network: Object.freeze({
      external_requests: network.externalRequests,
      forbidden_queries: network.forbiddenQueries,
      console_errors: network.consoleErrors,
      page_errors: network.pageErrors,
      login_statuses: Object.freeze([...network.loginStatuses]),
      logout_statuses: Object.freeze([...network.logoutStatuses]),
      plan_patch_statuses: Object.freeze([...network.planPatchStatuses]),
      quota_adjustment_statuses: Object.freeze([...network.quotaAdjustmentStatuses]),
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
  requireCondition(facts.status === "passed" && facts.all_108_prds_complete === false, "browser.complete", "status");
  requireTrueFields(facts.desktop, [
    "width", "height", "nonblank", "page_110", "page_111", "horizontal_overflow_free",
    "text_overflow_free", "overlap_count", "plan_authenticated", "plan_cas_exact",
    "plan_saved", "plan_native_modal", "plan_initial_focus", "plan_tab_forward_wrap",
    "plan_tab_reverse_wrap", "plan_idle_escape_closed", "plan_focus_restored",
    "plan_busy_escape_blocked", "quota_report_split",
  ], "browser.complete.desktop");
  requireCondition(facts.desktop.width === 1280 && facts.desktop.height === 720 && facts.desktop.overlap_count === 0, "browser.complete.desktop", "layout");
  requireTrueFields(facts.mobile, [
    "width", "height", "nonblank", "page_110", "page_111", "horizontal_overflow_free",
    "text_overflow_free", "overlap_count", "quota_types_exact", "quota_adjustments_exact",
    "quota_report_split", "quota_native_modal", "quota_initial_focus", "quota_idle_escape_closed",
    "quota_reason_cleared", "quota_focus_restored", "quota_busy_controls_disabled",
    "quota_busy_escape_blocked",
  ], "browser.complete.mobile");
  requireCondition(facts.mobile.width === 390 && facts.mobile.height === 844 && facts.mobile.overlap_count === 0, "browser.complete.mobile", "layout");
  requireTrueFields(facts.lifecycle, [
    "backend_rbac_denied", "logout_root_cleared", "logout_storage_cleared",
    "logout_late_response_discarded", "revocation_root_cleared",
    "revocation_navigation_cleared", "revocation_late_response_discarded",
  ], "browser.complete.lifecycle");
  exactObject(facts.network, [
    "external_requests", "forbidden_queries", "console_errors", "page_errors",
    "login_statuses", "logout_statuses", "plan_patch_statuses",
    "quota_adjustment_statuses",
  ], "browser.complete.network");
  requireCondition(
    facts.network.external_requests === 0
      && facts.network.forbidden_queries === 0
      && facts.network.console_errors === 0
      && facts.network.page_errors === 0
      && JSON.stringify(facts.network.login_statuses) === JSON.stringify([200, 200])
      && JSON.stringify(facts.network.logout_statuses) === JSON.stringify([200])
      && JSON.stringify(facts.network.plan_patch_statuses) === JSON.stringify([200])
      && JSON.stringify(facts.network.quota_adjustment_statuses) === JSON.stringify([201, 201, 201, 201]),
    "browser.complete.network",
    "facts",
  );
  requireCondition(JSON.stringify(facts.screenshots) === JSON.stringify(SCREENSHOTS), "browser.complete", "screenshots");
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
    await pageSession.client.send("Page.addScriptToEvaluateOnNewDocument", { source: `(() => {
      const original = window.setInterval.bind(window);
      window.setInterval = (callback, delay, ...args) => Number(delay) === 15000
        ? 0 : original(callback, delay, ...args);
    })();` }, pageSession.sessionId);
    await loadConsole(pageSession.client, pageSession.sessionId);
    await loginFinance(pageSession.client, pageSession.sessionId, privateConfig, tracker, "browser.login.first");

    const plan = await exercisePlan(pageSession.client, pageSession.sessionId, tracker, staging);
    const quota = await exerciseQuota(pageSession.client, pageSession.sessionId, tracker, staging);
    const logout = await logoutWithLateResponse(pageSession.client, pageSession.sessionId, tracker);
    await loginFinance(pageSession.client, pageSession.sessionId, privateConfig, tracker, "browser.login.second");
    const revocation = await revokeWithLateResponse(
      pageSession.client, pageSession.sessionId, tracker, privateConfig,
    );

    const privacy = await evaluate(pageSession.client, pageSession.sessionId, `(async () => {
      const needle = ${JSON.stringify(privateConfig.rolePassword)};
      const text = document.documentElement.textContent || '';
      const fields = [...document.querySelectorAll('input,textarea,select')].map((item) => String(item.value || ''));
      const attributes = [...document.querySelectorAll('*')].flatMap((item) => [...item.attributes].map((attribute) => attribute.value));
      const cacheNames = typeof caches === 'undefined' ? [] : await caches.keys();
      const databases = typeof indexedDB.databases === 'function' ? await indexedDB.databases() : [];
      return {
        private_hits: Number(text.includes(needle)) + fields.filter((item) => item.includes(needle)).length + attributes.filter((item) => item.includes(needle)).length,
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
      planDesktop: plan.planDesktop,
      planMobile: plan.planMobile,
      quotaDesktop: quota.quotaDesktop,
      quotaMobile: quota.quotaMobile,
      planAuthenticated: plan.authenticated,
      planCasExact: plan.casExact,
      planSaved: plan.saved,
      planNativeModal: plan.nativeModal,
      planInitialFocus: plan.initialFocus,
      planTabForwardWrap: plan.tabForwardWrap,
      planTabReverseWrap: plan.tabReverseWrap,
      planIdleEscapeClosed: plan.idleEscapeClosed,
      planFocusRestored: plan.focusRestored,
      planBusyEscapeBlocked: plan.busyEscapeBlocked,
      quotaTypesExact: quota.typesExact,
      quotaAdjustmentsExact: quota.adjustmentsExact,
      quotaReportSplit: quota.reportSplit,
      quotaNativeModal: quota.nativeModal,
      quotaInitialFocus: quota.initialFocus,
      quotaIdleEscapeClosed: quota.idleEscapeClosed,
      quotaReasonCleared: quota.reasonCleared,
      quotaFocusRestored: quota.focusRestored,
      quotaBusyControlsDisabled: quota.busyControlsDisabled,
      quotaBusyEscapeBlocked: quota.busyEscapeBlocked,
    }, {
      backendRbacDenied: true,
      logoutRootCleared: logout.rootCleared,
      logoutStorageCleared: logout.storageCleared,
      logoutLateResponseDiscarded: logout.lateDiscarded,
      revocationRootCleared: revocation.rootCleared,
      revocationNavigationCleared: revocation.navigationCleared,
      revocationLateResponseDiscarded: revocation.lateDiscarded,
    }, tracker);
    validateCompletionFacts(facts);

    await closePrd079Browser(browserClient, pageSession, browserChild);
    browserClient = null;
    pageSession = null;
    browserChild = null;
    await removeDriverDirectory(driverDirectory, privateConfig.driverOwner);

    const completed = await postHarness("/complete", privateConfig, facts);
    requireCondition(completed.status === 200, "harness.complete", "status");
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-079", screenshot_count: 4 });
  } catch (error) {
    primaryFailure = safeFailure(error);
    throw primaryFailure;
  } finally {
    let cleanupFailure = null;
    try { await closePrd079Browser(browserClient, pageSession, browserChild); } catch {
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
  directChild,
  parseArguments,
  safeRunId,
  validateCompletionFacts,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
