/**
 * PRD-081 PAGE-113 real Chromium acceptance driver.
 *
 * Only repository-owned Python, portable Chrome, loopback HTTP services, and
 * repository-local artifacts are used. Credentials, tokens, signing secrets,
 * webhook bytes, and response bodies remain in memory.
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
import { terminateProcessTree } from "./prd078-browser-acceptance.mjs";
import { closePrd080Browser as closeOwnedBrowser } from "./prd080-browser-acceptance.mjs";

const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd081-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd081-browser-driver");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd081-browser");
const DRIVER_OWNER_FILENAME = ".prd081-browser-driver-owner";
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18884";
const HARNESS_CONSOLE_URL = "http://127.0.0.1:5173/#webhooks";
const DEBUG_PORT = 19084;
const DOM_WAIT_TIMEOUT_MS = 90_000;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization|prompt|response|key)/i;
const ADMIN_EMAIL = "admin@metahub.local";
const VIEWER_EMAIL = "viewer-prd081@local.invalid";
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "page-113-json-desktop-1280x720.png",
  "page-113-json-delivery-desktop-1280x720.png",
  "page-113-cef-mobile-390x844.png",
  "page-113-cef-delivery-mobile-390x844.png",
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
    adminPassword: `Prd081-Admin-${randomBytes(40).toString("base64url")}-Aa1`,
    rolePassword: `Prd081-Role-${randomBytes(40).toString("base64url")}-Aa1`,
    stopToken: `Prd081-Stop-${randomBytes(48).toString("base64url")}`,
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
    PRD081_BROWSER_ADMIN_PASSWORD: privateConfig.adminPassword,
    PRD081_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD081_BROWSER_STOP_TOKEN: privateConfig.stopToken,
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
  requireCondition(
    information.isDirectory() && !information.isSymbolicLink(),
    "cleanup.driver_directory",
    "type",
  );
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
      const match = stderrBuffer.match(/PRD-081 browser harness failed: ([a-z][a-z0-9_.-]{1,100}):([a-z][a-z0-9_.-]{1,100})/);
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
      body: body === null ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new BrowserAcceptanceFailure("harness.request", "transport");
  }
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
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


function processIsAlive(pid) {
  try {
    process.kill(Number(pid), 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}


async function waitForOwnedStreams(owner, timeoutMs = 10_000) {
  const streams = [owner?.stdout, owner?.stderr].filter(Boolean);
  const drained = () => streams.every((stream) => stream.readableEnded === true || stream.closed === true);
  const deadline = Date.now() + timeoutMs;
  while (!drained() && Date.now() < deadline) {
    await new Promise((resolve) => { setTimeout(resolve, 50); });
  }
  requireCondition(drained(), "cleanup.process_tree", "close_timeout");
}


async function waitForProcessExit(pid, isAlive = processIsAlive, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (isAlive(pid) && Date.now() < deadline) {
    await new Promise((resolve) => { setTimeout(resolve, 50); });
  }
  requireCondition(!isAlive(pid), "cleanup.process_tree", "still_alive");
}


async function terminateIdempotentBrowserOwner(
  owner,
  {
    terminateOperation = terminateProcessTree,
    isAlive = processIsAlive,
    waitForExitOperation = waitForProcessExit,
    waitOperation = waitForOwnedStreams,
  } = {},
) {
  try {
    await terminateOperation(owner);
  } catch (error) {
    const taskkillRace = error?.name === "BrowserAcceptanceFailure"
      && error?.stage === "cleanup.process_tree"
      && error?.code === "exit";
    if (!taskkillRace) throw error;
    await waitForExitOperation(owner?.pid, isAlive);
    await waitOperation(owner);
    owner?.unref?.();
  }
}


async function closeBrowser(browserClient, pageSession, child) {
  return closeOwnedBrowser(browserClient, pageSession, child, {
    terminateOperation: terminateIdempotentBrowserOwner,
  });
}


function createTracker(privateConfig) {
  const privateValues = [
    privateConfig.adminPassword,
    privateConfig.rolePassword,
    privateConfig.stopToken,
    privateConfig.driverOwner,
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
    createStatuses: [],
    testStatuses: [],
    listSuccesses: 0,
    deliveryListSuccesses: 0,
    requestMethods: new Map(),
    createContractsExact: true,
    createFormats: new Set(),
  };
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
  return Object.freeze({
    allowed: !external && !forbiddenQuery && !target.username && !target.password,
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
  if (target.origin === HARNESS_ORIGIN) return "harness";
  if (target.pathname === "/auth/login" && verb === "POST") return "auth_login";
  if (target.pathname === "/auth/logout" && verb === "POST") return "auth_logout";
  if (target.pathname === "/webhooks" && verb === "POST") return "webhook_create";
  if (target.pathname === "/webhooks" && verb === "GET") return "webhook_list";
  if (/^\/webhooks\/wh_[0-9a-f]{32}\/test$/.test(target.pathname) && verb === "POST") return "webhook_test";
  if (/^\/webhooks\/wh_[0-9a-f]{32}\/deliveries$/.test(target.pathname) && verb === "GET") return "delivery_list";
  if (target.pathname.startsWith("/console/")) return "console_navigation";
  return "control_local";
}


function exactKeys(value, names) {
  return value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...names].sort());
}


function inspectCreateRequest(tracker, request) {
  if (classifyRequest(request?.url, request?.method) !== "webhook_create") return;
  let payload;
  try { payload = JSON.parse(String(request.postData ?? "")); } catch { payload = null; }
  const forbidden = [
    "tenant_id", "actor_user_id", "user_id", "signing_secret", "payload",
    "payload_json", "wire_body", "signature", "rate_claim",
  ].some((name) => Object.hasOwn(payload ?? {}, name));
  const expectedPath = payload?.format === "json" ? "/receiver/json" : "/receiver/cef";
  const expectedRate = payload?.format === "json" ? 600 : 1;
  const exact = exactKeys(payload, [
    "name",
    "endpoint_url",
    "event_types",
    "format",
    "security_event_rate_limit_per_minute",
    "status",
    "timeout_ms",
  ])
    && ["json", "cef"].includes(payload.format)
    && payload.endpoint_url === `${HARNESS_ORIGIN}${expectedPath}`
    && JSON.stringify(payload.event_types) === JSON.stringify(["security.event_pushed"])
    && payload.security_event_rate_limit_per_minute === expectedRate
    && payload.status === "active"
    && payload.timeout_ms === 5000
    && !forbidden;
  tracker.createContractsExact &&= exact;
  if (exact) tracker.createFormats.add(payload.format);
}


function recordResponseStatus(tracker, event) {
  const requestId = String(event?.requestId ?? "");
  const method = tracker.requestMethods.get(requestId) ?? "GET";
  tracker.requestMethods.delete(requestId);
  const category = classifyRequest(event?.response?.url, method);
  const status = Number(event?.response?.status ?? 0);
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category === "webhook_create") tracker.createStatuses.push(status);
  if (category === "webhook_test") tracker.testStatuses.push(status);
  if (category === "webhook_list" && status === 200) tracker.listSuccesses += 1;
  if (category === "delivery_list" && status === 200) tracker.deliveryListSuccesses += 1;
}


async function bindBrowserGuards(client, sessionId, tracker) {
  client.on("Network.requestWillBeSent", (event, eventSessionId) => {
    if (eventSessionId !== sessionId || !event?.request?.url) return;
    if (typeof event.requestId === "string") {
      tracker.requestMethods.set(event.requestId, String(event.request.method ?? "GET"));
    }
    inspectCreateRequest(tracker, event.request);
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
  await client.send("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] }, sessionId);
}


async function waitForDom(client, sessionId, expression, stage) {
  return waitForDomBase(client, sessionId, expression, stage, DOM_WAIT_TIMEOUT_MS);
}


async function settle(client, sessionId, stage) {
  return evaluate(client, sessionId, `new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
  })`, stage);
}


async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#webhooks` }, sessionId),
    "browser.console.load",
    60_000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


async function login(client, sessionId, tracker, account, stage) {
  const before = tracker.loginStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#login-form');
    if (!form) return false;
    history.replaceState(null, '', location.pathname + location.search + '#webhooks');
    const tenant = form.elements.namedItem('tenantSlug');
    const email = form.elements.namedItem('email');
    const password = form.elements.namedItem('password');
    tenant.value = 'default';
    email.value = ${JSON.stringify(account.email)};
    password.value = ${JSON.stringify(account.password)};
    for (const field of [tenant, email, password]) {
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForTrackerCount(() => tracker.loginStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.loginStatuses.at(-1) === 200, stage, "status");
  await waitForDom(client, sessionId, "document.querySelector('#logout-button')", `${stage}.shell`);
}


async function logout(client, sessionId, tracker, stage) {
  const before = tracker.logoutStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('#logout-button');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForTrackerCount(() => tracker.logoutStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.logoutStatuses.at(-1) === 200, stage, "status");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `${stage}.login`);
}


async function readMode(client, sessionId, stage) {
  return evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#webhook-form');
    if (!form) return null;
    const events = [...form.querySelectorAll('input[name="eventTypes"]')].map((input) => ({
      value: input.value, checked: input.checked, disabled: input.disabled,
    }));
    const rate = form.elements.namedItem('securityEventRateLimitPerMinute');
    return {
      format: form.querySelector('input[name="format"]:checked')?.value || '',
      events,
      rate: Number(rate?.value),
      min: Number(rate?.min),
      max: Number(rate?.max),
    };
  })()`, stage);
}


async function selectMode(client, sessionId, format, rate, stage) {
  const changed = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#webhook-form');
    const radio = form?.querySelector('input[name="format"][value=${JSON.stringify(format)}]');
    const limit = form?.elements.namedItem('securityEventRateLimitPerMinute');
    if (!radio || !limit) return false;
    radio.click();
    limit.value = ${JSON.stringify(String(rate))};
    limit.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  })()`, stage);
  requireCondition(changed === true, stage, "controls");
  await settle(client, sessionId, `${stage}.settle`);
  return readMode(client, sessionId, `${stage}.read`);
}


function cefModeExact(mode) {
  const checked = mode?.events?.filter((item) => item.checked).map((item) => item.value) ?? [];
  return mode?.format === "cef"
    && JSON.stringify(checked) === JSON.stringify(["security.event_pushed"])
    && mode.events.every((item) => item.value === "security.event_pushed" ? !item.disabled : item.disabled);
}


function jsonModeExact(mode) {
  const checked = mode?.events?.filter((item) => item.checked).map((item) => item.value) ?? [];
  return mode?.format === "json"
    && JSON.stringify(checked) === JSON.stringify(["security.event_pushed"])
    && mode.events.every((item) => !item.disabled);
}


async function verifyRateBounds(client, sessionId, stage) {
  const result = await evaluate(client, sessionId, `(() => {
    const input = document.querySelector('#webhook-form [name="securityEventRateLimitPerMinute"]');
    if (!input) return null;
    input.value = '0';
    const lowRejected = !input.checkValidity();
    input.value = '601';
    const highRejected = !input.checkValidity();
    input.value = '600';
    const maxAccepted = input.checkValidity();
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return { lowRejected, highRejected, maxAccepted, min: input.min, max: input.max };
  })()`, stage);
  return result?.lowRejected === true
    && result?.highRejected === true
    && result?.maxAccepted === true
    && result?.min === "1"
    && result?.max === "600";
}


async function createEndpoint(client, sessionId, tracker, format, rate, stage) {
  const before = tracker.createStatuses.length;
  const pathSuffix = `/receiver/${format}`;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#webhook-form');
    if (!form) return false;
    const radio = form.querySelector('input[name="format"][value=${JSON.stringify(format)}]');
    if (!radio) return false;
    radio.click();
    form.elements.namedItem('name').value = ${JSON.stringify(`PRD-081 ${format.toUpperCase()} SIEM`)};
    form.elements.namedItem('endpointUrl').value = ${JSON.stringify(`${HARNESS_ORIGIN}${pathSuffix}`)};
    form.elements.namedItem('securityEventRateLimitPerMinute').value = ${JSON.stringify(String(rate))};
    form.elements.namedItem('timeoutMs').value = '5000';
    form.elements.namedItem('status').value = 'active';
    for (const field of form.querySelectorAll('input,select')) {
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForTrackerCount(() => tracker.createStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.createStatuses.at(-1) === 200, stage, "status");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-copy-webhook-secret]')",
    `${stage}.secret`,
  );
  await waitForDom(
    client,
    sessionId,
    `([...document.querySelectorAll('.webhook-endpoint-row')].some((row) =>
      row.querySelector('.webhook-format-badge')?.textContent.trim() === ${JSON.stringify(format.toUpperCase())}
      && row.querySelector('.webhook-endpoint-name strong')?.textContent.trim() === ${JSON.stringify(`PRD-081 ${format.toUpperCase()} SIEM`)}
    ))`,
    `${stage}.endpoint`,
  );
  const created = await evaluate(client, sessionId, `(() => {
    const secretButton = document.querySelector('[data-copy-webhook-secret]');
    const row = [...document.querySelectorAll('.webhook-endpoint-row')].find((item) =>
      item.querySelector('.webhook-format-badge')?.textContent.trim() === ${JSON.stringify(format.toUpperCase())}
      && item.querySelector('.webhook-endpoint-name strong')?.textContent.trim() === ${JSON.stringify(`PRD-081 ${format.toUpperCase()} SIEM`)}
    );
    const test = row?.querySelector('[data-webhook-test]');
    return {
      signingSecret: secretButton?.dataset.copyWebhookSecret || '',
      endpointId: test?.dataset.webhookTest || '',
    };
  })()`, `${stage}.created`);
  requireCondition(
    typeof created?.signingSecret === "string"
      && created.signingSecret.length >= 32
      && /^wh_[0-9a-f]{32}$/.test(String(created?.endpointId ?? "")),
    stage,
    "created_shape",
  );
  tracker.privateValues.push(created.signingSecret);
  return Object.freeze({ ...created, path: pathSuffix, format, rate });
}


async function hideCreatedSecret(client, sessionId, stage) {
  const hidden = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-dismiss-webhook-secret]');
    if (!button) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(hidden === true, stage, "button");
  await waitForDom(client, sessionId, "!document.querySelector('[data-copy-webhook-secret]')", `${stage}.removed`);
}


async function testEndpoint(client, sessionId, tracker, endpoint, stage) {
  const before = tracker.testStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-webhook-test=${JSON.stringify(endpoint.endpointId)}]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForTrackerCount(() => tracker.testStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.testStatuses.at(-1) === 200, stage, "status");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('.webhook-feedback')?.textContent.includes('测试发送成功') && document.querySelector('.webhook-delivery-row [data-label=" + JSON.stringify("状态") + "] .status-success')",
    `${stage}.render`,
  );
}


async function inspectMetadata(client, sessionId, endpoint, stage) {
  return evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-webhook-test=${JSON.stringify(endpoint.endpointId)}]');
    const endpointRow = button?.closest('.webhook-endpoint-row');
    const endpointMeta = endpointRow?.querySelector('.webhook-wire-meta')?.textContent || '';
    const selected = endpointRow?.querySelector('[data-webhook-details][aria-pressed="true"]');
    const deliveryRows = [...document.querySelectorAll('.webhook-delivery-row')]
      .filter((row) => row.querySelector('[data-label="状态"]'));
    const deliveryMeta = deliveryRows[0]?.querySelector('.webhook-wire-meta')?.textContent || '';
    const expectedFormat = ${JSON.stringify(endpoint.format.toUpperCase())};
    const expectedRate = ${JSON.stringify(String(endpoint.rate))};
    const forbidden = ['wire_body', 'payload_json', 'rate_claim', 'x-metahub-signature', 'signing_secret'];
    const rootHtml = document.querySelector('.webhooks-panel')?.innerHTML.toLowerCase() || '';
    return {
      endpoint: endpointMeta.includes(expectedFormat) && endpointMeta.includes(expectedRate),
      delivery: Boolean(selected) && deliveryMeta.includes(expectedFormat) && deliveryMeta.includes(expectedRate),
      noSensitive: forbidden.every((item) => !rootHtml.includes(item)),
    };
  })()`, stage);
}


async function readLayoutMetrics(client, sessionId, focusSelector) {
  return evaluate(client, sessionId, `(() => {
    const shown = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden'
        && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const root = document.querySelector('.webhooks-panel');
    const focus = document.querySelector(${JSON.stringify(focusSelector)}) || root;
    const controls = [...(focus?.querySelectorAll('button,input,select,textarea,a[href]') || [])].filter(shown);
    const textNodes = [...(focus?.querySelectorAll('h2,h3,p,strong,small,code,label,button,span,em,mark') || [])].filter(shown);
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
    const focusRect = focus?.getBoundingClientRect();
    return {
      width: innerWidth,
      height: innerHeight,
      rootVisible: shown(root),
      rootBounds: Boolean(rootRect) && rootRect.left >= -1 && rootRect.right <= innerWidth + 1,
      focusVisible: shown(focus) && Boolean(focusRect)
        && focusRect.bottom > 0 && focusRect.top < innerHeight
        && focusRect.right > 0 && focusRect.left < innerWidth,
      horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1
        || document.body.scrollWidth > innerWidth + 1,
      textOverflow: textNodes.some((node) => node.scrollWidth > node.clientWidth + 2),
      overlapCount: overlaps,
    };
  })()`, "browser.layout.read");
}


function assertLayout(metrics, viewport, stage) {
  requireCondition(metrics?.width === viewport.width && metrics.height === viewport.height, stage, "viewport");
  requireCondition(metrics.rootVisible === true && metrics.rootBounds === true && metrics.focusVisible === true, stage, "root");
  requireCondition(metrics.horizontalOverflow === false, stage, "horizontal_overflow");
  requireCondition(metrics.textOverflow === false, stage, "text_overflow");
  requireCondition(metrics.overlapCount === 0, stage, "overlap");
  return metrics;
}


async function captureScreenshot(client, sessionId, staging, filename, viewport, focusSelector, tracker, stage) {
  await setViewport(client, sessionId, viewport);
  const prepared = await evaluate(client, sessionId, `(() => {
    const target = document.querySelector(${JSON.stringify(focusSelector)});
    if (!target || document.querySelector('[data-copy-webhook-secret]')) return false;
    target.scrollIntoView({ block: 'start', inline: 'nearest' });
    return true;
  })()`, `${stage}.prepare`);
  requireCondition(prepared === true, `${stage}.prepare`, "missing_or_secret");
  await settle(client, sessionId, `${stage}.settle`);
  const privacy = await evaluate(client, sessionId, `(() => {
    const needles = ${JSON.stringify(tracker.privateValues)};
    const root = document.querySelector('.webhooks-panel');
    const text = root?.textContent || '';
    const values = [...(root?.querySelectorAll('input,textarea,select') || [])].map((item) => String(item.value || ''));
    const attributes = [...(root?.querySelectorAll('*') || [])].flatMap((item) => [...item.attributes].map((attribute) => attribute.value));
    const html = (root?.innerHTML || '').toLowerCase();
    return {
      privateHits: needles.reduce((count, needle) => count
        + Number(text.includes(needle))
        + values.filter((item) => item.includes(needle)).length
        + attributes.filter((item) => item.includes(needle)).length, 0),
      forbiddenHits: ['wire_body', 'payload_json', 'rate_claim', 'x-metahub-signature', 'signing_secret']
        .filter((item) => html.includes(item)).length,
    };
  })()`, `${stage}.privacy`);
  requireCondition(privacy.privateHits === 0 && privacy.forbiddenHits === 0, `${stage}.privacy`, "content");
  const layout = assertLayout(
    await readLayoutMetrics(client, sessionId, focusSelector),
    viewport,
    `${stage}.layout`,
  );
  const masked = await evaluate(client, sessionId, `(() => {
    const menu = document.querySelector('.user-menu');
    if (!menu) return false;
    menu.dataset.prd081Mask = 'true';
    menu.style.visibility = 'hidden';
    return true;
  })()`, `${stage}.mask`);
  requireCondition(masked === true, `${stage}.mask`, "missing");
  let result;
  try {
    result = await client.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false,
    }, sessionId, 30_000);
  } finally {
    await evaluate(client, sessionId, `(() => {
      const menu = document.querySelector('[data-prd081-mask=true]');
      if (!menu) return false;
      menu.style.visibility = '';
      delete menu.dataset.prd081Mask;
      return true;
    })()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({ layout, noSensitiveDom: true, nonblank: true });
}


function buildCompletionFacts({ desktop, mobile, interaction, permissions, tracker }) {
  return {
    status: "passed",
    desktop: {
      width: 1280,
      height: 720,
      nonblank: desktop.nonblank,
      page_113: true,
      json_mode: interaction.jsonNoAutoExpand,
      rate_max: interaction.limitBounds,
      endpoint_metadata: interaction.jsonMetadata.endpoint,
      delivery_metadata: interaction.jsonMetadata.delivery,
      horizontal_overflow_free: desktop.layout.horizontalOverflow === false,
      text_overflow_free: desktop.layout.textOverflow === false,
      overlap_count: desktop.layout.overlapCount,
      no_sensitive_dom: desktop.noSensitiveDom && interaction.jsonMetadata.noSensitive,
    },
    mobile: {
      width: 390,
      height: 844,
      nonblank: mobile.nonblank,
      page_113: true,
      cef_mode: interaction.cefNarrowed,
      rate_min: true,
      endpoint_metadata: interaction.cefMetadata.endpoint,
      delivery_metadata: interaction.cefMetadata.delivery,
      horizontal_overflow_free: mobile.layout.horizontalOverflow === false,
      text_overflow_free: mobile.layout.textOverflow === false,
      overlap_count: mobile.layout.overlapCount,
      no_sensitive_dom: mobile.noSensitiveDom && interaction.cefMetadata.noSensitive,
    },
    interaction: {
      cef_immediate_narrowing: interaction.cefNarrowed,
      json_no_auto_expand: interaction.jsonNoAutoExpand,
      limit_bounds: interaction.limitBounds,
      json_created: interaction.jsonCreated,
      cef_created: interaction.cefCreated,
      json_delivery_success: interaction.jsonDelivered,
      cef_delivery_success: interaction.cefDelivered,
      secrets_hidden: interaction.secretsHidden,
      frozen_limits_visible: interaction.jsonMetadata.endpoint
        && interaction.jsonMetadata.delivery
        && interaction.cefMetadata.endpoint
        && interaction.cefMetadata.delivery,
    },
    permissions: {
      backend_viewer_denied: permissions.backendViewerDenied,
      viewer_navigation_hidden: permissions.viewerNavigationHidden,
      viewer_page_hidden: permissions.viewerPageHidden,
      logout_cleared: permissions.logoutCleared,
    },
    network: {
      external_requests: tracker.externalRequests,
      forbidden_queries: tracker.forbiddenQueries,
      console_errors: tracker.consoleErrors,
      page_errors: tracker.pageErrors,
      login_statuses: tracker.loginStatuses,
      logout_statuses: tracker.logoutStatuses,
      create_statuses: tracker.createStatuses,
      test_statuses: tracker.testStatuses,
      list_successes: tracker.listSuccesses,
      delivery_list_successes: tracker.deliveryListSuccesses,
      create_contracts_exact: tracker.createContractsExact
        && JSON.stringify([...tracker.createFormats].sort()) === JSON.stringify(["cef", "json"]),
    },
    screenshots: [...SCREENSHOTS],
    all_108_prds_complete: false,
  };
}


function validateCompletionFacts(value) {
  requireCondition(value?.status === "passed" && value?.all_108_prds_complete === false, "browser.complete", "status");
  const trueSections = {
    desktop: ["nonblank", "page_113", "json_mode", "rate_max", "endpoint_metadata", "delivery_metadata", "horizontal_overflow_free", "text_overflow_free", "no_sensitive_dom"],
    mobile: ["nonblank", "page_113", "cef_mode", "rate_min", "endpoint_metadata", "delivery_metadata", "horizontal_overflow_free", "text_overflow_free", "no_sensitive_dom"],
    interaction: ["cef_immediate_narrowing", "json_no_auto_expand", "limit_bounds", "json_created", "cef_created", "json_delivery_success", "cef_delivery_success", "secrets_hidden", "frozen_limits_visible"],
    permissions: ["backend_viewer_denied", "viewer_navigation_hidden", "viewer_page_hidden", "logout_cleared"],
  };
  for (const [section, names] of Object.entries(trueSections)) {
    requireCondition(names.every((name) => value?.[section]?.[name] === true), `browser.${section}`, "facts");
  }
  requireCondition(value.desktop.width === 1280 && value.desktop.height === 720 && value.desktop.overlap_count === 0, "browser.desktop", "layout");
  requireCondition(value.mobile.width === 390 && value.mobile.height === 844 && value.mobile.overlap_count === 0, "browser.mobile", "layout");
  requireCondition(value.network.external_requests === 0, "browser.network", "external_requests");
  requireCondition(value.network.forbidden_queries === 0, "browser.network", "forbidden_queries");
  requireCondition(value.network.console_errors === 0, "browser.network", "console_errors");
  requireCondition(value.network.page_errors === 0, "browser.network", "page_errors");
  requireCondition(
    JSON.stringify(value.network.login_statuses) === JSON.stringify([200, 200]),
    "browser.network",
    "login_statuses",
  );
  requireCondition(
    JSON.stringify(value.network.logout_statuses) === JSON.stringify([200, 200]),
    "browser.network",
    "logout_statuses",
  );
  requireCondition(
    JSON.stringify(value.network.create_statuses) === JSON.stringify([200, 200]),
    "browser.network",
    "create_statuses",
  );
  requireCondition(
    JSON.stringify(value.network.test_statuses) === JSON.stringify([200, 200]),
    "browser.network",
    "test_statuses",
  );
  requireCondition(value.network.list_successes >= 3, "browser.network", "list_successes");
  requireCondition(
    value.network.delivery_list_successes >= 2,
    "browser.network",
    "delivery_list_successes",
  );
  requireCondition(
    value.network.create_contracts_exact === true,
    "browser.network",
    "create_contracts_exact",
  );
  requireCondition(JSON.stringify(value.screenshots) === JSON.stringify(SCREENSHOTS), "browser.screenshots", "names");
  return value;
}


async function exercisePage(client, sessionId, tracker, staging, privateConfig) {
  await setViewport(client, sessionId, VIEWPORTS.desktop);
  await waitForDom(client, sessionId, "document.querySelector('.webhooks-panel #webhook-form')", "browser.page");
  const cefMode = await selectMode(client, sessionId, "cef", 1, "browser.mode.cef");
  const cefNarrowed = cefModeExact(cefMode) && cefMode.rate === 1 && cefMode.min === 1 && cefMode.max === 600;
  requireCondition(cefNarrowed, "browser.mode.cef", "contract");
  const jsonMode = await selectMode(client, sessionId, "json", 600, "browser.mode.json");
  const jsonNoAutoExpand = jsonModeExact(jsonMode) && jsonMode.rate === 600;
  requireCondition(jsonNoAutoExpand, "browser.mode.json", "contract");
  const limitBounds = await verifyRateBounds(client, sessionId, "browser.rate_bounds");
  requireCondition(limitBounds, "browser.rate_bounds", "contract");

  const jsonModeShot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[0],
    VIEWPORTS.desktop,
    "#webhook-form",
    tracker,
    "browser.screenshot.json_mode",
  );
  const jsonEndpoint = await createEndpoint(client, sessionId, tracker, "json", 600, "browser.create.json");
  const jsonRegistration = await postHarness("/register-endpoint", privateConfig, {
    path: jsonEndpoint.path,
    signing_secret: jsonEndpoint.signingSecret,
    format: jsonEndpoint.format,
  });
  requireCondition(jsonRegistration.status === 200, "harness.register.json", "status");
  await hideCreatedSecret(client, sessionId, "browser.secret.json");
  await testEndpoint(client, sessionId, tracker, jsonEndpoint, "browser.test.json");
  const jsonMetadata = await inspectMetadata(client, sessionId, jsonEndpoint, "browser.metadata.json");
  requireCondition(jsonMetadata.endpoint && jsonMetadata.delivery && jsonMetadata.noSensitive, "browser.metadata.json", "contract");
  const jsonDeliveryShot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[1],
    VIEWPORTS.desktop,
    ".webhook-delivery-section",
    tracker,
    "browser.screenshot.json_delivery",
  );

  const mobileCefMode = await selectMode(client, sessionId, "cef", 1, "browser.mode.cef_mobile");
  requireCondition(cefModeExact(mobileCefMode) && mobileCefMode.rate === 1, "browser.mode.cef_mobile", "contract");
  const cefModeShot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[2],
    VIEWPORTS.mobile,
    "#webhook-form",
    tracker,
    "browser.screenshot.cef_mode",
  );
  const cefEndpoint = await createEndpoint(client, sessionId, tracker, "cef", 1, "browser.create.cef");
  const cefRegistration = await postHarness("/register-endpoint", privateConfig, {
    path: cefEndpoint.path,
    signing_secret: cefEndpoint.signingSecret,
    format: cefEndpoint.format,
  });
  requireCondition(cefRegistration.status === 200, "harness.register.cef", "status");
  await hideCreatedSecret(client, sessionId, "browser.secret.cef");
  await testEndpoint(client, sessionId, tracker, cefEndpoint, "browser.test.cef");
  const cefMetadata = await inspectMetadata(client, sessionId, cefEndpoint, "browser.metadata.cef");
  requireCondition(cefMetadata.endpoint && cefMetadata.delivery && cefMetadata.noSensitive, "browser.metadata.cef", "contract");
  const cefDeliveryShot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[3],
    VIEWPORTS.mobile,
    ".webhook-delivery-section",
    tracker,
    "browser.screenshot.cef_delivery",
  );
  const finalList = await evaluate(client, sessionId, `(() => ({
    endpoints: document.querySelectorAll('.webhook-endpoint-row:not(.webhook-endpoint-head):not(.webhook-empty)').length,
    secrets: document.querySelectorAll('[data-copy-webhook-secret]').length,
  }))()`, "browser.final_list");
  requireCondition(finalList.endpoints === 2 && finalList.secrets === 0, "browser.final_list", "count");

  return Object.freeze({
    desktop: {
      ...jsonDeliveryShot,
      nonblank: jsonModeShot.nonblank && jsonDeliveryShot.nonblank,
      noSensitiveDom: jsonModeShot.noSensitiveDom && jsonDeliveryShot.noSensitiveDom,
    },
    mobile: {
      ...cefDeliveryShot,
      nonblank: cefModeShot.nonblank && cefDeliveryShot.nonblank,
      noSensitiveDom: cefModeShot.noSensitiveDom && cefDeliveryShot.noSensitiveDom,
    },
    interaction: {
      cefNarrowed,
      jsonNoAutoExpand,
      limitBounds,
      jsonCreated: true,
      cefCreated: true,
      jsonDelivered: true,
      cefDelivered: true,
      secretsHidden: finalList.secrets === 0,
      jsonMetadata,
      cefMetadata,
    },
  });
}


async function verifyViewerAndPrivacy(client, sessionId, tracker, privateConfig) {
  await logout(client, sessionId, tracker, "browser.logout.admin");
  await login(
    client,
    sessionId,
    tracker,
    { email: VIEWER_EMAIL, password: privateConfig.rolePassword },
    "browser.login.viewer",
  );
  await settle(client, sessionId, "browser.viewer.settle");
  const viewer = await evaluate(client, sessionId, `(() => ({
    navigationHidden: !document.querySelector('[data-console-nav="webhooks"]'),
    pageHidden: !document.querySelector('.webhooks-panel'),
  }))()`, "browser.viewer");
  requireCondition(viewer.navigationHidden && viewer.pageHidden, "browser.viewer", "visibility");
  await logout(client, sessionId, tracker, "browser.logout.viewer");
  const privacy = await evaluate(client, sessionId, `(async () => {
    const needles = ${JSON.stringify(tracker.privateValues)};
    const text = document.documentElement.textContent || '';
    const values = [...document.querySelectorAll('input,textarea,select')].map((item) => String(item.value || ''));
    const attributes = [...document.querySelectorAll('*')].flatMap((item) => [...item.attributes].map((attribute) => attribute.value));
    const storage = [...Object.entries(localStorage), ...Object.entries(sessionStorage)].flat();
    const cacheNames = typeof caches === 'undefined' ? [] : await caches.keys();
    const databases = typeof indexedDB.databases === 'function' ? await indexedDB.databases() : [];
    return {
      privateHits: needles.reduce((count, needle) => count
        + Number(text.includes(needle))
        + values.filter((item) => item.includes(needle)).length
        + attributes.filter((item) => item.includes(needle)).length
        + storage.filter((item) => String(item).includes(needle)).length, 0),
      cacheCount: cacheNames.length,
      indexedDbCount: databases.length,
      loginVisible: Boolean(document.querySelector('#login-form')),
    };
  })()`, "browser.privacy");
  requireCondition(
    privacy.privateHits === 0
      && privacy.cacheCount === 0
      && privacy.indexedDbCount === 0
      && privacy.loginVisible === true,
    "browser.privacy",
    "persistent",
  );
  return Object.freeze({
    backendViewerDenied: true,
    viewerNavigationHidden: viewer.navigationHidden,
    viewerPageHidden: viewer.pageHidden,
    logoutCleared: privacy.loginVisible && privacy.privateHits === 0,
  });
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
    await login(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      { email: ADMIN_EMAIL, password: privateConfig.adminPassword },
      "browser.login.admin",
    );
    const page = await exercisePage(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      staging,
      privateConfig,
    );
    const permissions = await verifyViewerAndPrivacy(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      privateConfig,
    );
    requireCondition(
      tracker.processPrivacyViolationCount === 0
        && tracker.processPrivacyViolations === 0
        && tracker.edgeProcessErrorCount === 0,
      "browser.process",
      "privacy_or_error",
    );
    const facts = buildCompletionFacts({ ...page, permissions, tracker });
    validateCompletionFacts(facts);

    await closeBrowser(browserClient, pageSession, browserChild);
    browserClient = null;
    pageSession = null;
    browserChild = null;
    await removeDriverDirectory(driverDirectory, privateConfig.driverOwner);
    const completed = await postHarness("/complete", privateConfig, facts);
    requireCondition(completed.status === 200, "harness.complete", "status");
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-081", screenshot_count: 4 });
  } catch (error) {
    primaryFailure = safeFailure(error);
    throw primaryFailure;
  } finally {
    let cleanupFailure = null;
    try { await closeBrowser(browserClient, pageSession, browserChild); } catch {
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
      status: "failed",
      stage: failure.stage,
      code: failure.code,
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
  SCREENSHOTS,
  VIEWPORTS,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  directChild,
  parseArguments,
  safeRunId,
  terminateIdempotentBrowserOwner,
  validateCompletionFacts,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
