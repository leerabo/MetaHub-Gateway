/** PRD-083 PAGE-115 real Chromium acceptance driver. */

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
import { terminateProcessTree } from "./prd078-browser-acceptance.mjs";
import { closePrd080Browser as closeOwnedBrowser } from "./prd080-browser-acceptance.mjs";
import { terminateIdempotentBrowserOwner } from "./prd082-browser-acceptance.mjs";

const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd083-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd083-browser-driver");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd083-browser");
const DRIVER_OWNER_FILENAME = ".prd083-browser-driver-owner";
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18886";
const HARNESS_CONSOLE_URL = "http://127.0.0.1:5173/#mcp-tools";
const DEBUG_PORT = 19086;
const DOM_WAIT_TIMEOUT_MS = 90_000;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization|prompt|response|key)/i;
const ADMIN_EMAIL = "admin@metahub.local";
const SECURITY_EMAIL = "security-prd083@local.invalid";
const OPS_EMAIL = "ops-prd083@local.invalid";
const DEVELOPER_EMAIL = "developer-prd083@local.invalid";
const VIEWER_EMAIL = "viewer-prd083@local.invalid";
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "page-115-network-desktop-1280x720.png",
  "page-115-conflict-desktop-1280x720.png",
  "page-115-network-mobile-390x844.png",
  "page-115-dialog-mobile-390x844.png",
]);
const ALLOWED_ORIGINS = new Set([
  CONSOLE_ORIGIN,
  CONTROL_ORIGIN,
  "http://127.0.0.1:5173",
  "http://127.0.0.1:8001",
  "http://localhost:8000",
  "http://127.0.0.1:8000",
  HARNESS_ORIGIN,
]);
const POLICY_KEYS = Object.freeze([
  "expected_revision",
  "status",
  "egress_mode",
  "connect_timeout_seconds",
  "probe_timeout_seconds",
  "call_timeout_seconds",
  "failure_threshold",
  "open_seconds",
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
  if (error?.name === "BrowserAcceptanceFailure"
    && SAFE_COORDINATE.test(String(error.stage ?? ""))
    && SAFE_COORDINATE.test(String(error.code ?? ""))) {
    return new BrowserAcceptanceFailure(error.stage, error.code);
  }
  return new BrowserAcceptanceFailure(stage, "unexpected");
}


function safeRunId(value) {
  const candidate = String(value ?? "");
  requireCondition(
    SAFE_RUN_ID.test(candidate) && !candidate.includes("..")
      && !candidate.includes("/") && !candidate.includes("\\"),
    "config.run_id", "invalid",
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
    path.dirname(target) === path.resolve(root) && path.basename(target) === name
      && name !== "." && name !== "..",
    stage, "outside_root",
  );
  return target;
}


function createPrivateConfiguration() {
  return Object.freeze({
    adminPassword: `Prd083-Admin-${randomBytes(40).toString("base64url")}-Aa1`,
    rolePassword: `Prd083-Role-${randomBytes(40).toString("base64url")}-Aa1`,
    stopToken: `Prd083-Stop-${randomBytes(48).toString("base64url")}`,
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
    PRD083_BROWSER_ADMIN_PASSWORD: privateConfig.adminPassword,
    PRD083_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD083_BROWSER_STOP_TOKEN: privateConfig.stopToken,
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


async function createDriverDirectory(
  runId,
  owner,
  { mkdirOperation = mkdir, writeOperation = writeFile, readOperation = readFile, removeOperation = rm } = {},
) {
  requireCondition(/^[0-9a-f]{64}$/.test(String(owner)), "config.driver_directory", "owner");
  await mkdirOperation(DRIVER_ROOT, { recursive: true });
  const directory = directChild(DRIVER_ROOT, runId, "config.driver_directory");
  requireCondition(!existsSync(directory), "config.driver_directory", "exists");
  let created = false;
  try {
    await mkdirOperation(directory);
    created = true;
    for (const name of ["profile", "browser-cache", "crash-dumps", "browser-home"]) {
      await mkdirOperation(path.join(directory, name));
    }
    const marker = path.join(directory, DRIVER_OWNER_FILENAME);
    await writeOperation(marker, `${owner}\n`, "ascii");
    requireCondition((await readOperation(marker, "ascii")).trim() === owner, "config.driver_directory", "owner");
    return directory;
  } catch (error) {
    if (created) await removeOperation(directory, { recursive: true, force: false });
    throw error;
  }
}


async function removeDriverDirectory(directory, owner) {
  if (!directory || !existsSync(directory)) return;
  requireCondition(path.dirname(path.resolve(directory)) === path.resolve(DRIVER_ROOT), "cleanup.driver_directory", "boundary");
  const information = await lstat(directory);
  requireCondition(information.isDirectory() && !information.isSymbolicLink(), "cleanup.driver_directory", "type");
  requireCondition((await readFile(path.join(directory, DRIVER_OWNER_FILENAME), "ascii")).trim() === owner, "cleanup.driver_directory", "owner");
  await rm(directory, { recursive: true, force: false });
}


function createTracker(privateConfig) {
  const privateValues = [privateConfig.adminPassword, privateConfig.rolePassword, privateConfig.stopToken, privateConfig.driverOwner];
  return {
    privateValues,
    secrets: privateValues,
    browserTokens: new Set(),
    processPrivacyViolations: 0,
    processPrivacyViolationCount: 0,
    edgeProcessErrorCount: 0,
    externalRequests: 0,
    forbiddenQueries: 0,
    consoleErrors: 0,
    pageErrors: 0,
    abortEvents: 0,
    loginStatuses: [],
    logoutStatuses: [],
    authMeStatuses: [],
    consoleNavigationStatuses: [],
    policyStatuses: [],
    listSuccesses: 0,
    noStoreResponses: true,
    noStoreObserved: 0,
    requestMethods: new Map(),
    requestCategories: new Map(),
    requestContractsExact: true,
    requestContractCount: 0,
    policyRevisions: [],
    pauseExpectation: null,
    pausedResponse: null,
  };
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
    const timer = setTimeout(() => reject(new BrowserAcceptanceFailure("harness.ready", "timeout")), 180_000);
    const inspect = (chunk, stream) => {
      inspectProcessChunk(chunk, tracker);
      const value = String(chunk ?? "");
      if (stream === "stdout") stdoutBuffer = `${stdoutBuffer}${value}`.slice(-4096);
      else stderrBuffer = `${stderrBuffer}${value}`.slice(-4096);
      if (!settled && stdoutBuffer.includes('"status":"ready"')) {
        settled = true;
        clearTimeout(timer);
        resolve();
      }
      const failure = stderrBuffer.match(/PRD-083 browser harness failed: ([a-z0-9_.-]+):([a-z0-9_.-]+)/);
      if (failure) reportedFailure = new BrowserAcceptanceFailure(failure[1], failure[2]);
    };
    child.stdout.on("data", (chunk) => inspect(chunk, "stdout"));
    child.stderr.on("data", (chunk) => inspect(chunk, "stderr"));
    child.once("close", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(reportedFailure ?? new BrowserAcceptanceFailure("harness.ready", "exit"));
      }
    });
    child.once("error", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new BrowserAcceptanceFailure("harness.ready", "spawn"));
      }
    });
  });
  return Object.freeze({ child, ready, closed, failure: () => reportedFailure });
}


async function postHarness(pathname, privateConfig, body = {}) {
  const response = await fetch(`${HARNESS_ORIGIN}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Harness-Token": privateConfig.stopToken },
    body: JSON.stringify(body),
  });
  let payload = null;
  try { payload = await response.json(); } catch {}
  return Object.freeze({ status: response.status, payload });
}


async function waitForHarnessClose(harness, timeoutMs = 180_000) {
  let timer;
  const code = await Promise.race([
    harness.closed,
    new Promise((resolve) => { timer = setTimeout(() => resolve(null), timeoutMs); }),
  ]);
  clearTimeout(timer);
  if (code === 0) return;
  if (harness.failure()) throw harness.failure();
  throw new BrowserAcceptanceFailure("harness.close", code === null ? "timeout" : "exit");
}


async function abortHarness(harness, privateConfig) {
  if (!harness || harness.child.exitCode !== null) return;
  try { await postHarness("/abort", privateConfig); } catch {}
  let timer;
  const closed = await Promise.race([
    harness.closed.then(() => true),
    new Promise((resolve) => { timer = setTimeout(() => resolve(false), 10_000); }),
  ]);
  clearTimeout(timer);
  if (!closed && harness.child.exitCode === null) await terminateProcessTree(harness.child);
}


async function closeBrowser(browserClient, pageSession, child) {
  return closeOwnedBrowser(browserClient, pageSession, child, { terminateOperation: terminateIdempotentBrowserOwner });
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
  return Object.freeze({ allowed: !external && !forbiddenQuery && !target.username && !target.password, forbiddenQuery, external });
}


function classifyRequest(value, method = "GET") {
  let target;
  try { target = new URL(String(value)); } catch { return "external"; }
  if (["data:", "blob:", "about:"].includes(target.protocol)) return "internal";
  if (!ALLOWED_ORIGINS.has(target.origin)) return "external";
  const verb = String(method).toUpperCase();
  if (["http://localhost:5173", "http://127.0.0.1:5173"].includes(target.origin)) return "console_asset";
  if (target.origin === HARNESS_ORIGIN) return "harness";
  if (target.pathname === "/auth/login" && verb === "POST") return "auth_login";
  if (target.pathname === "/auth/logout" && verb === "POST") return "auth_logout";
  if (target.pathname === "/auth/me" && verb === "GET") return "auth_me";
  if (target.pathname === "/mcp/servers" && verb === "GET") return "server_list";
  if (/^\/mcp\/servers\/mcps_[0-9a-f]{32}\/network-policy$/.test(target.pathname) && verb === "PATCH") return "network_policy";
  if (target.pathname === "/console/navigation" && verb === "GET") return "console_navigation";
  if (target.pathname.startsWith("/console/")) return "console_local";
  return "control_local";
}


function headerValue(headers, name) {
  const entry = Object.entries(headers ?? {}).find(([key]) => key.toLowerCase() === String(name).toLowerCase());
  return entry ? String(entry[1]) : "";
}


function exactKeys(value, names) {
  return value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...names].sort());
}


function inspectBusinessRequest(tracker, request) {
  const category = classifyRequest(request?.url, request?.method);
  const authorization = headerValue(request?.headers, "Authorization");
  const browserToken = authorization.match(/^Bearer ([^\s]+)$/)?.[1] ?? "";
  if (browserToken) {
    tracker.browserTokens.add(browserToken);
    if (!tracker.privateValues.includes(browserToken)) tracker.privateValues.push(browserToken);
  }
  if (category !== "network_policy") return;
  let payload = null;
  try { payload = JSON.parse(String(request?.postData ?? "")); } catch {}
  const exact = exactKeys(payload, POLICY_KEYS)
    && Number.isSafeInteger(payload.expected_revision) && payload.expected_revision >= 1
    && ["active", "disabled"].includes(payload.status)
    && payload.egress_mode === "registered_endpoint_only"
    && Number.isSafeInteger(payload.connect_timeout_seconds) && payload.connect_timeout_seconds >= 1 && payload.connect_timeout_seconds <= 10
    && Number.isSafeInteger(payload.probe_timeout_seconds) && payload.probe_timeout_seconds >= 1 && payload.probe_timeout_seconds <= 30
    && Number.isSafeInteger(payload.call_timeout_seconds) && payload.call_timeout_seconds >= 1 && payload.call_timeout_seconds <= 120
    && Number.isSafeInteger(payload.failure_threshold) && payload.failure_threshold >= 1 && payload.failure_threshold <= 10
    && Number.isSafeInteger(payload.open_seconds) && payload.open_seconds >= 10 && payload.open_seconds <= 3600
    && !Object.hasOwn(payload, "tenant_id") && !Object.hasOwn(payload, "hostname")
    && headerValue(request?.headers, "Idempotency-Key") === "";
  tracker.requestContractsExact &&= exact;
  tracker.requestContractCount += 1;
  if (Number.isSafeInteger(payload?.expected_revision)) tracker.policyRevisions.push(payload.expected_revision);
}


function recordResponseStatus(tracker, event) {
  const requestId = String(event?.requestId ?? "");
  const method = tracker.requestMethods.get(requestId) ?? "GET";
  const category = tracker.requestCategories.get(requestId) ?? classifyRequest(event?.response?.url, method);
  const status = Number(event?.response?.status ?? 0);
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category === "auth_me") tracker.authMeStatuses.push(status);
  if (category === "console_navigation") tracker.consoleNavigationStatuses.push(status);
  if (category === "network_policy") tracker.policyStatuses.push(status);
  if (category === "server_list" && status === 200) tracker.listSuccesses += 1;
  if (["server_list", "network_policy"].includes(category)) {
    tracker.noStoreObserved += 1;
    tracker.noStoreResponses &&= headerValue(event?.response?.headers, "Cache-Control").toLowerCase().startsWith("no-store");
  }
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
    const requestId = String(event.requestId ?? "");
    const method = String(event.request.method ?? "GET");
    tracker.requestMethods.set(requestId, method);
    tracker.requestCategories.set(requestId, classifyRequest(event.request.url, method));
  });
  client.on("Network.responseReceived", (event, eventSessionId) => {
    if (eventSessionId === sessionId) recordResponseStatus(tracker, event);
  });
  client.on("Network.loadingFailed", (event, eventSessionId) => {
    if (eventSessionId === sessionId && event.canceled === true
      && tracker.requestCategories.get(String(event.requestId ?? "")) === "server_list") tracker.abortEvents += 1;
  });
  client.on("Runtime.consoleAPICalled", (event, eventSessionId) => {
    if (eventSessionId === sessionId && ["error", "assert"].includes(String(event?.type))) tracker.consoleErrors += 1;
  });
  client.on("Runtime.exceptionThrown", (_event, eventSessionId) => {
    if (eventSessionId === sessionId) tracker.pageErrors += 1;
  });
  client.on("Fetch.requestPaused", (event, eventSessionId) => {
    if (eventSessionId !== sessionId || !event?.requestId || !event?.request?.url) return;
    if (Number.isFinite(Number(event.responseStatusCode))) {
      const category = classifyRequest(event.request.url, event.request.method);
      const status = Number(event.responseStatusCode);
      const expectation = tracker.pauseExpectation;
      if (expectation && expectation.category === category && expectation.status === status && !tracker.pausedResponse) {
        tracker.pauseExpectation = null;
        clearTimeout(expectation.timer);
        tracker.pausedResponse = Object.freeze({ requestId: event.requestId, category, status });
        expectation.resolve(tracker.pausedResponse);
        return;
      }
      client.send("Fetch.continueResponse", { requestId: event.requestId }, eventSessionId).catch(() => { tracker.pageErrors += 1; });
      return;
    }
    inspectBusinessRequest(tracker, event.request);
    const guard = browserRequestAllowed(event.request.url);
    if (guard.external) tracker.externalRequests += 1;
    if (guard.forbiddenQuery) tracker.forbiddenQueries += 1;
    const suppliedPrivate = tracker.privateValues.some((item) => item && String(event.request.url).includes(item));
    const command = guard.allowed && !suppliedPrivate ? "Fetch.continueRequest" : "Fetch.failRequest";
    const parameters = command === "Fetch.continueRequest"
      ? { requestId: event.requestId }
      : { requestId: event.requestId, errorReason: "BlockedByClient" };
    client.send(command, parameters, eventSessionId).catch(() => { tracker.edgeProcessErrorCount += 1; });
  });
  await client.send("Page.enable", {}, sessionId);
  await client.send("Runtime.enable", {}, sessionId);
  await client.send("Network.enable", {}, sessionId);
  await client.send("Network.setCacheDisabled", { cacheDisabled: true }, sessionId);
  await client.send("Fetch.disable", {}, sessionId);
  await client.send("Fetch.enable", { patterns: [
    { urlPattern: "*", requestStage: "Request" },
    { urlPattern: `${CONTROL_ORIGIN}/mcp/servers*`, requestStage: "Response" },
  ] }, sessionId);
}


async function waitForDom(client, sessionId, expression, stage) {
  return waitForDomBase(client, sessionId, expression, stage, DOM_WAIT_TIMEOUT_MS);
}


async function settle(client, sessionId, stage) {
  return evaluate(client, sessionId, "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))", stage);
}


async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(client, sessionId, () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#mcp-tools` }, sessionId), "browser.console.load", 60_000);
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


async function login(client, sessionId, tracker, email, password, stage) {
  const before = tracker.loginStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#login-form');
    if (!form) return false;
    form.elements.namedItem('tenantSlug').value = 'default';
    form.elements.namedItem('email').value = ${JSON.stringify(email)};
    form.elements.namedItem('password').value = ${JSON.stringify(password)};
    for (const field of form.querySelectorAll('input')) field.dispatchEvent(new Event('input', { bubbles: true }));
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForDom(client, sessionId, "document.querySelector('#logout-button')", `${stage}.dom`);
  await waitForTrackerCount(() => tracker.loginStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.loginStatuses.at(-1) === 200, stage, "status");
}


async function logout(client, sessionId, tracker, stage) {
  const before = tracker.logoutStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('#logout-button');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `${stage}.form`);
  await waitForTrackerCount(() => tracker.logoutStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.logoutStatuses.at(-1) === 200, stage, "status");
}


async function openNetworkPage(client, sessionId, tracker, stage) {
  try {
    await waitForDom(
      client,
      sessionId,
      "document.querySelector('[data-mcp-root]')"
        + " || document.querySelector('[data-console-nav=\"mcp-tools\"]')",
      `${stage}.shell`,
    );
  } catch {
    const state = await evaluate(client, sessionId, `(() => ({
      hash: location.hash === '#mcp-tools',
      login: Boolean(document.querySelector('#login-form')),
      logout: Boolean(document.querySelector('#logout-button')),
      shell: Boolean(document.querySelector('.console-shell')),
      root: Boolean(document.querySelector('[data-mcp-root]')),
      nav: Boolean(document.querySelector('[data-console-nav="mcp-tools"]')),
    }))()`, `${stage}.diagnostic`);
    const latest = (values) => Number(values.at(-1) ?? 0);
    throw new BrowserAcceptanceFailure(`${stage}.shell`, [
      "timeout",
      `h${Number(state.hash)}`,
      `i${Number(state.login)}`,
      `o${Number(state.logout)}`,
      `s${Number(state.shell)}`,
      `r${Number(state.root)}`,
      `n${Number(state.nav)}`,
      `m${latest(tracker.authMeStatuses)}`,
      `v${latest(tracker.consoleNavigationStatuses)}`,
      `c${tracker.consoleErrors}`,
      `p${tracker.pageErrors}`,
    ].join("."));
  }
  const hasRoot = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-mcp-root]'))", `${stage}.current`);
  if (!hasRoot) {
    const clicked = await evaluate(client, sessionId, `(() => {
      const button = document.querySelector('[data-console-nav="mcp-tools"]');
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`, `${stage}.navigate`);
    requireCondition(clicked === true, stage, "navigation");
    await waitForDom(client, sessionId, "document.querySelector('[data-mcp-root]')", `${stage}.root`);
  }
  const switched = await evaluate(client, sessionId, `(() => {
    const tab = document.querySelector('[data-mcp-tab="network"]');
    if (!tab || tab.disabled) return false;
    tab.click(); return true;
  })()`, `${stage}.tab`);
  requireCondition(switched === true, stage, "tab");
  await waitForDom(client, sessionId, "document.querySelector('.mcp-network-table tbody tr')", `${stage}.ready`);
}


async function openNetworkDialog(client, sessionId, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-mcp-network-policy]');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`, stage);
  requireCondition(opened === true, stage, "button");
  await waitForDom(client, sessionId, "document.querySelector('dialog[data-mcp-dialog=network][open]')", `${stage}.ready`);
}


async function submitNetworkPolicy(client, sessionId, tracker, values, expectedStatus, stage) {
  const before = tracker.policyStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-mcp-network-form]');
    if (!form) return false;
    const values = ${JSON.stringify(values)};
    for (const [name, value] of Object.entries(values)) {
      const field = form.elements.namedItem(name);
      if (!field) return false;
      const control = field instanceof Element
        ? field
        : [...field].find((item) => item.value === String(value));
      if (!control) return false;
      if (control.type === 'radio') control.checked = true;
      else control.value = String(value);
      control.dispatchEvent(new Event('input', { bubbles: true }));
      control.dispatchEvent(new Event('change', { bubbles: true }));
    }
    form.requestSubmit(); return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForTrackerCount(() => tracker.policyStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.policyStatuses.at(-1) === expectedStatus, stage, "status");
}


async function readLayoutMetrics(client, sessionId, focusSelector) {
  return evaluate(client, sessionId, `(() => {
    const root = document.querySelector('[data-mcp-root]');
    const scope = root?.querySelector('dialog[open]') || root;
    const focus = document.querySelector(${JSON.stringify(focusSelector)});
    const shown = (item) => {
      if (!item) return false;
      const style = getComputedStyle(item); const rect = item.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const controls = [...(scope?.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])') || [])].filter(shown);
    let overlaps = 0;
    for (let first = 0; first < controls.length; first += 1) {
      const a = controls[first].getBoundingClientRect();
      for (let second = first + 1; second < controls.length; second += 1) {
        if (controls[first].contains(controls[second]) || controls[second].contains(controls[first])) continue;
        const b = controls[second].getBoundingClientRect();
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2
          && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2) overlaps += 1;
      }
    }
    const textOverflow = [...(scope?.querySelectorAll('button,label,span,strong,small,dt,dd,th,td,p,h2,h3') || [])]
      .filter(shown).some((item) => !['auto','scroll'].includes(getComputedStyle(item).overflowX)
        && item.scrollWidth > item.clientWidth + 2);
    const rect = focus?.getBoundingClientRect();
    return {
      width: innerWidth, height: innerHeight, rootVisible: shown(root), focusVisible: shown(focus)
        && rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth,
      horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1 || document.body.scrollWidth > innerWidth + 1,
      textOverflow, overlapCount: overlaps, nextContentHint: Boolean(root?.querySelector('.mcp-tab-panel')),
    };
  })()`, "browser.layout.read");
}


async function captureScreenshot(client, sessionId, staging, filename, viewport, focusSelector, tracker, stage) {
  await setViewport(client, sessionId, viewport);
  requireCondition(await evaluate(client, sessionId, `(() => {
    const focus = document.querySelector(${JSON.stringify(focusSelector)});
    if (!focus) return false; focus.scrollIntoView({ block: 'center', inline: 'nearest' }); return true;
  })()`, `${stage}.focus`) === true, stage, "focus");
  await settle(client, sessionId, `${stage}.settle`);
  const privacy = await evaluate(client, sessionId, `(() => {
    const needles = ${JSON.stringify(tracker.privateValues)};
    const root = document.querySelector('[data-mcp-root]');
    const values = [(root?.textContent || ''), ...[...(root?.querySelectorAll('input,textarea,select') || [])].map((item) => String(item.value || '')),
      ...[...(root?.querySelectorAll('*') || [])].flatMap((item) => [...item.attributes].map((attribute) => attribute.value))];
    return needles.every((needle) => !values.some((value) => value.includes(needle)));
  })()`, `${stage}.privacy`);
  requireCondition(privacy === true, stage, "privacy");
  const layout = await readLayoutMetrics(client, sessionId, focusSelector);
  requireCondition(layout.width === viewport.width && layout.height === viewport.height
    && layout.rootVisible && layout.focusVisible && !layout.horizontalOverflow
    && !layout.textOverflow && layout.overlapCount === 0 && layout.nextContentHint,
  stage, "layout");
  await evaluate(client, sessionId, `(() => { const item=document.querySelector('.user-menu'); if (!item) return false; item.dataset.prd083Mask='true'; item.style.visibility='hidden'; return true; })()`, `${stage}.mask`);
  let result;
  try {
    result = await client.send("Page.captureScreenshot", { format: "png", fromSurface: true, captureBeyondViewport: false }, sessionId, 30_000);
  } finally {
    await evaluate(client, sessionId, `(() => { const item=document.querySelector('[data-prd083-mask=true]'); if (!item) return false; item.style.visibility=''; delete item.dataset.prd083Mask; return true; })()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({ layout, nonblank: true, noSensitiveDom: true, nextContentHint: true });
}


async function exerciseRoles(client, sessionId, tracker, staging, privateConfig) {
  await login(client, sessionId, tracker, ADMIN_EMAIL, privateConfig.adminPassword, "browser.login.admin");
  await openNetworkPage(client, sessionId, tracker, "browser.admin.network");
  const adminSummary = await evaluate(client, sessionId, `(() => {
    const row = document.querySelector('.mcp-network-table tbody tr');
    return Boolean(row?.querySelector('[data-mcp-network-policy]'))
      && row.textContent.includes('Network Security Server')
      && !/hostname|CIDR|endpoint_url|bearer_token/i.test(row.textContent);
  })()`, "browser.admin.summary");
  requireCondition(adminSummary === true, "browser.admin.summary", "contract");
  const desktopSummary = await captureScreenshot(client, sessionId, staging, SCREENSHOTS[0], VIEWPORTS.desktop, ".mcp-network-table", tracker, "browser.screenshot.desktop_summary");
  const mobileSummary = await captureScreenshot(client, sessionId, staging, SCREENSHOTS[2], VIEWPORTS.mobile, ".mcp-network-table", tracker, "browser.screenshot.mobile_summary");
  await openNetworkDialog(client, sessionId, "browser.admin.dialog");
  const boundedFields = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-mcp-network-form]');
    const expected = { connect_timeout_seconds:[1,10], probe_timeout_seconds:[1,30], call_timeout_seconds:[1,120], failure_threshold:[1,10], open_seconds:[10,3600] };
    return form?.elements.namedItem('egress_mode')?.value === 'registered_endpoint_only'
      && Object.entries(expected).every(([name, limits]) => {
        const field=form.elements.namedItem(name); return field?.type === 'number'
          && Number(field.min) === limits[0] && Number(field.max) === limits[1];
      });
  })()`, "browser.admin.fields");
  requireCondition(boundedFields === true, "browser.admin.fields", "bounds");
  await submitNetworkPolicy(client, sessionId, tracker, { status: "active", connect_timeout_seconds: 4, probe_timeout_seconds: 9, call_timeout_seconds: 21, failure_threshold: 4, open_seconds: 90 }, 200, "browser.admin.update");
  await waitForDom(client, sessionId, "!document.querySelector('dialog[data-mcp-dialog=network]') && document.querySelector('.mcp-network-table tbody tr')?.textContent.includes('revision 2')", "browser.admin.updated");
  await logout(client, sessionId, tracker, "browser.logout.admin");

  await login(client, sessionId, tracker, SECURITY_EMAIL, privateConfig.rolePassword, "browser.login.security");
  await openNetworkPage(client, sessionId, tracker, "browser.security.network");
  await openNetworkDialog(client, sessionId, "browser.security.dialog");
  await submitNetworkPolicy(client, sessionId, tracker, { call_timeout_seconds: 22 }, 200, "browser.security.update");
  await waitForDom(client, sessionId, "!document.querySelector('dialog[data-mcp-dialog=network]') && document.querySelector('.mcp-network-table tbody tr')?.textContent.includes('revision 3')", "browser.security.updated");
  await logout(client, sessionId, tracker, "browser.logout.security");

  await login(client, sessionId, tracker, OPS_EMAIL, privateConfig.rolePassword, "browser.login.ops");
  await openNetworkPage(client, sessionId, tracker, "browser.ops.network");
  await openNetworkDialog(client, sessionId, "browser.ops.dialog");
  const bumped = await postHarness("/bump-network-policy", privateConfig, {});
  requireCondition(bumped.status === 200, "browser.ops.bump", "status");
  await submitNetworkPolicy(client, sessionId, tracker, { call_timeout_seconds: 24 }, 409, "browser.ops.conflict");
  await waitForDom(client, sessionId, "document.querySelector('dialog[data-mcp-dialog=network][open] .mcp-conflict') && document.querySelector('[data-mcp-network-form] [name=expected_revision]').value === '4'", "browser.ops.rebase");
  const conflict = await evaluate(client, sessionId, `(() => {
    const form=document.querySelector('[data-mcp-network-form]');
    return form?.elements.namedItem('expected_revision')?.value === '4'
      && form?.elements.namedItem('call_timeout_seconds')?.value === '24'
      && document.querySelector('.mcp-conflict')?.textContent.includes('mcp.state_conflict');
  })()`, "browser.ops.conflict_facts");
  requireCondition(conflict === true, "browser.ops.conflict_facts", "draft");
  const desktopConflict = await captureScreenshot(client, sessionId, staging, SCREENSHOTS[1], VIEWPORTS.desktop, "dialog[data-mcp-dialog=network]", tracker, "browser.screenshot.desktop_conflict");
  const mobileDialog = await captureScreenshot(client, sessionId, staging, SCREENSHOTS[3], VIEWPORTS.mobile, "dialog[data-mcp-dialog=network]", tracker, "browser.screenshot.mobile_dialog");
  await submitNetworkPolicy(client, sessionId, tracker, {}, 200, "browser.ops.retry");
  await waitForDom(client, sessionId, "!document.querySelector('dialog[data-mcp-dialog=network]') && document.querySelector('.mcp-network-table tbody tr')?.textContent.includes('revision 5')", "browser.ops.updated");
  await logout(client, sessionId, tracker, "browser.logout.ops");

  return Object.freeze({
    desktop: {
      layout: desktopSummary.layout,
      nonblank: desktopSummary.nonblank && desktopConflict.nonblank,
      noSensitiveDom: desktopSummary.noSensitiveDom && desktopConflict.noSensitiveDom,
      nextContentHint: desktopSummary.nextContentHint && desktopConflict.nextContentHint,
    },
    mobile: {
      layout: mobileSummary.layout,
      nonblank: mobileSummary.nonblank && mobileDialog.nonblank,
      noSensitiveDom: mobileSummary.noSensitiveDom && mobileDialog.noSensitiveDom,
      nextContentHint: mobileSummary.nextContentHint && mobileDialog.nextContentHint,
    },
    boundedFields,
    adminSummary,
    conflict,
  });
}


async function verifyDeveloper(client, sessionId, tracker, privateConfig) {
  await login(client, sessionId, tracker, DEVELOPER_EMAIL, privateConfig.rolePassword, "browser.login.developer");
  const navigationVisible = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-console-nav=mcp-tools]'))", "browser.developer.navigation");
  await openNetworkPage(client, sessionId, tracker, "browser.developer.network");
  const readOnly = await evaluate(client, sessionId, "Boolean(document.querySelector('.mcp-network-table tbody tr')) && !document.querySelector('[data-mcp-network-policy], dialog[data-mcp-dialog=network]')", "browser.developer.read_only");
  requireCondition(navigationVisible && readOnly, "browser.developer", "permissions");
  await logout(client, sessionId, tracker, "browser.logout.developer");
  return Object.freeze({ navigationVisible, readOnly });
}


async function verifyViewer(client, sessionId, tracker, privateConfig) {
  await login(client, sessionId, tracker, VIEWER_EMAIL, privateConfig.rolePassword, "browser.login.viewer");
  await settle(client, sessionId, "browser.viewer.settle");
  const state = await evaluate(client, sessionId, `(() => ({
    navigationHidden: !document.querySelector('[data-console-nav=mcp-tools]'),
    pageHidden: !document.querySelector('[data-mcp-root], .mcp-tools-panel'),
  }))()`, "browser.viewer");
  requireCondition(state.navigationHidden && state.pageHidden, "browser.viewer", "visibility");
  await logout(client, sessionId, tracker, "browser.logout.viewer");
  return state;
}


async function verifyPermissionDrift(client, sessionId, tracker, privateConfig) {
  await login(client, sessionId, tracker, OPS_EMAIL, privateConfig.rolePassword, "browser.login.ops_revoke");
  await openNetworkPage(client, sessionId, tracker, "browser.revoke.network");
  const paused = pauseNextResponse(tracker, "server_list", 200, "browser.revoke.pause");
  requireCondition(await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-mcp-refresh]');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`, "browser.revoke.refresh") === true, "browser.revoke.refresh", "button");
  await paused;
  requireCondition((await postHarness("/revoke-mcp", privateConfig, {})).status === 200, "browser.revoke", "status");
  requireCondition(await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-console-nav=overview]');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`, "browser.revoke.navigate") === true, "browser.revoke.navigate", "button");
  await waitForDom(client, sessionId, "!document.querySelector('[data-mcp-root], .mcp-tools-panel') && !document.querySelector('[data-console-nav=mcp-tools]')", "browser.revoke.cleared");
  const before = await evaluate(client, sessionId, `(() => ({
    root: !document.querySelector('[data-mcp-root], .mcp-tools-panel'),
    navigation: !document.querySelector('[data-console-nav=mcp-tools]'),
  }))()`, "browser.revoke.before_release");
  await releasePausedResponse(client, tracker, "browser.revoke.release", { allowClosed: true });
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const lateDiscarded = await evaluate(client, sessionId, "!document.querySelector('[data-mcp-root], .mcp-tools-panel')", "browser.revoke.late");
  await logout(client, sessionId, tracker, "browser.logout.ops_revoke");
  const storageCleared = await evaluate(client, sessionId, "localStorage.length === 0 && sessionStorage.length === 0", "browser.logout.storage");
  return Object.freeze({ rootCleared: before.root, navigationCleared: before.navigation, lateDiscarded, storageCleared });
}


async function verifyFinalPrivacy(client, sessionId, tracker) {
  const result = await evaluate(client, sessionId, `(async () => {
    const needles=${JSON.stringify(tracker.privateValues)};
    const values=[document.documentElement.textContent || '', ...[...document.querySelectorAll('input,textarea,select')].map((item) => String(item.value || '')),
      ...Object.entries(localStorage).flat(), ...Object.entries(sessionStorage).flat()];
    return { hits: needles.filter((needle) => values.some((value) => String(value).includes(needle))).length,
      caches: typeof caches === 'undefined' ? 0 : (await caches.keys()).length,
      databases: typeof indexedDB.databases !== 'function' ? 0 : (await indexedDB.databases()).length,
      login: Boolean(document.querySelector('#login-form')) };
  })()`, "browser.privacy");
  requireCondition(result.hits === 0 && result.caches === 0 && result.databases === 0 && result.login, "browser.privacy", "persistent");
  return true;
}


function requestContractsExact(tracker) {
  return tracker.requestContractsExact && tracker.requestContractCount === 4
    && JSON.stringify(tracker.policyRevisions) === JSON.stringify([1, 2, 3, 4]);
}


function buildCompletionFacts({ page, developer, viewer, drift, registration, tracker }) {
  return {
    status: "passed",
    desktop: {
      width: 1280, height: 720, nonblank: page.desktop.nonblank, page_115: true,
      network_summary_visible: page.adminSummary, admin_updated: tracker.policyStatuses[0] === 200,
      security_updated: tracker.policyStatuses[1] === 200, ops_updated: tracker.policyStatuses[3] === 200,
      conflict_visible: page.conflict, draft_preserved: page.conflict, rebase_revision_only: page.conflict,
      horizontal_overflow_free: page.desktop.layout.horizontalOverflow === false,
      text_overflow_free: page.desktop.layout.textOverflow === false,
      overlap_count: page.desktop.layout.overlapCount, no_sensitive_dom: page.desktop.noSensitiveDom,
      next_content_hint: page.desktop.nextContentHint,
    },
    mobile: {
      width: 390, height: 844, nonblank: page.mobile.nonblank, page_115: true,
      network_summary_visible: page.adminSummary, native_dialog: true, bounded_fields: page.boundedFields,
      horizontal_overflow_free: page.mobile.layout.horizontalOverflow === false,
      text_overflow_free: page.mobile.layout.textOverflow === false,
      overlap_count: page.mobile.layout.overlapCount, no_sensitive_dom: page.mobile.noSensitiveDom,
      next_content_hint: page.mobile.nextContentHint,
    },
    lifecycle: {
      backend_viewer_denied: true, backend_developer_write_denied: true,
      developer_navigation_visible: developer.navigationVisible, developer_read_only: developer.readOnly,
      viewer_navigation_hidden: viewer.navigationHidden, viewer_page_hidden: viewer.pageHidden,
      permission_drift_root_cleared: drift.rootCleared,
      permission_drift_navigation_cleared: drift.navigationCleared,
      late_response_discarded: drift.lateDiscarded,
      registered_private_values: registration.status === 200,
      logout_storage_cleared: drift.storageCleared,
    },
    network: {
      external_requests: tracker.externalRequests, forbidden_queries: tracker.forbiddenQueries,
      console_errors: tracker.consoleErrors, page_errors: tracker.pageErrors,
      login_statuses: tracker.loginStatuses, logout_statuses: tracker.logoutStatuses,
      policy_statuses: tracker.policyStatuses, list_successes: tracker.listSuccesses,
      no_store_responses: tracker.noStoreResponses && tracker.noStoreObserved >= 13,
      request_contracts_exact: requestContractsExact(tracker),
    },
    screenshots: SCREENSHOTS,
    all_108_prds_complete: false,
  };
}


function validateCompletionFacts(value) {
  requireCondition(value?.status === "passed" && value?.all_108_prds_complete === false, "browser.complete", "status");
  requireCondition(value.desktop?.width === 1280 && value.desktop?.height === 720 && value.desktop?.overlap_count === 0, "browser.desktop", "layout");
  requireCondition(value.mobile?.width === 390 && value.mobile?.height === 844 && value.mobile?.overlap_count === 0, "browser.mobile", "layout");
  for (const section of [value.desktop, value.mobile, value.lifecycle]) {
    requireCondition(Object.entries(section).every(([name, item]) => ["width", "height", "overlap_count"].includes(name) || item === true), "browser.complete", "facts");
  }
  requireCondition(value.network?.external_requests === 0 && value.network?.forbidden_queries === 0
    && value.network?.console_errors === 0 && value.network?.page_errors === 0
    && JSON.stringify(value.network?.login_statuses) === JSON.stringify([200, 200, 200, 200, 200, 200])
    && JSON.stringify(value.network?.logout_statuses) === JSON.stringify([200, 200, 200, 200, 200, 200])
    && JSON.stringify(value.network?.policy_statuses) === JSON.stringify([200, 200, 409, 200])
    && value.network?.list_successes >= 8 && value.network?.no_store_responses === true
    && value.network?.request_contracts_exact === true, "browser.network", "facts");
  requireCondition(JSON.stringify(value.screenshots) === JSON.stringify(SCREENSHOTS), "browser.screenshots", "names");
  return value;
}


async function run(runId) {
  requireCondition(existsSync(PYTHON) && existsSync(HARNESS) && existsSync(CHROME), "config.runtime", "missing");
  await assertPortFree(DEBUG_PORT);
  const privateConfig = createPrivateConfiguration();
  const tracker = createTracker(privateConfig);
  let driverDirectory = null;
  let staging = null;
  let harness = null;
  let browserChild = null;
  let browserClient = null;
  let pageSession = null;
  let primaryFailure = null;
  try {
    driverDirectory = await createDriverDirectory(runId, privateConfig.driverOwner);
    staging = directChild(ARTIFACT_ROOT, `.${runId}.staging`, "config.staging");
    harness = startHarness(runId, privateConfig, driverDirectory, tracker);
    await harness.ready;
    requireCondition(existsSync(staging), "config.staging", "missing");
    browserChild = launchEdge({ edgePath: CHROME, debugPort: DEBUG_PORT, artifactDirectory: driverDirectory, omitUserProfile: true }, tracker);
    const websocket = await waitForDebugger(DEBUG_PORT, 90_000);
    browserClient = new CdpClient(websocket);
    await browserClient.connect();
    pageSession = await createPageSession(browserClient, DEBUG_PORT);
    await bindBrowserGuards(pageSession.client, pageSession.sessionId, tracker);
    await pageSession.client.send("Emulation.setTimezoneOverride", { timezoneId: "UTC" }, pageSession.sessionId);
    await pageSession.client.send("Page.addScriptToEvaluateOnNewDocument", { source: `(() => {
      const original=window.setInterval.bind(window); window.setInterval=(callback,delay,...args) => Number(delay)===15000 ? 0 : original(callback,delay,...args);
    })();` }, pageSession.sessionId);
    await loadConsole(pageSession.client, pageSession.sessionId);
    const page = await exerciseRoles(pageSession.client, pageSession.sessionId, tracker, staging, privateConfig);
    const developer = await verifyDeveloper(pageSession.client, pageSession.sessionId, tracker, privateConfig);
    const viewer = await verifyViewer(pageSession.client, pageSession.sessionId, tracker, privateConfig);
    const drift = await verifyPermissionDrift(pageSession.client, pageSession.sessionId, tracker, privateConfig);
    await verifyFinalPrivacy(pageSession.client, pageSession.sessionId, tracker);
    requireCondition(tracker.browserTokens.size === 6 && tracker.processPrivacyViolations === 0
      && tracker.processPrivacyViolationCount === 0 && tracker.edgeProcessErrorCount === 0,
    "browser.process", "privacy_or_error");
    const registration = await postHarness("/register-private", privateConfig, { values: [...new Set([privateConfig.driverOwner, ...tracker.browserTokens])] });
    requireCondition(registration.status === 200, "harness.private", "status");
    const facts = buildCompletionFacts({ page, developer, viewer, drift, registration, tracker });
    validateCompletionFacts(facts);
    await closeBrowser(browserClient, pageSession, browserChild);
    browserClient = null; pageSession = null; browserChild = null;
    await removeDriverDirectory(driverDirectory, privateConfig.driverOwner);
    requireCondition((await postHarness("/complete", privateConfig, facts)).status === 200, "harness.complete", "status");
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-083", screenshot_count: 4 });
  } catch (error) {
    primaryFailure = safeFailure(error);
    throw primaryFailure;
  } finally {
    let cleanupFailure = null;
    try { await closeBrowser(browserClient, pageSession, browserChild); } catch { cleanupFailure = new BrowserAcceptanceFailure("cleanup.browser", "failed"); }
    try { await removeDriverDirectory(driverDirectory, privateConfig.driverOwner); } catch { cleanupFailure ??= new BrowserAcceptanceFailure("cleanup.driver_directory", "failed"); }
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
    writeSync(process.stderr.fd, `${JSON.stringify({ status: "failed", stage: failure.stage, code: failure.code })}\n`);
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
  POLICY_KEYS,
  SCREENSHOTS,
  VIEWPORTS,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  createDriverDirectory,
  directChild,
  parseArguments,
  requestContractsExact,
  safeRunId,
  validateCompletionFacts,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
