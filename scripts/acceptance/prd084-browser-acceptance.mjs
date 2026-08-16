/** PRD-084 PAGE-116 real Chromium acceptance driver. */

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
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd084-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd084-browser-driver");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd084-browser");
const DRIVER_OWNER_FILENAME = ".prd084-browser-driver-owner";
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18887";
const HARNESS_CONSOLE_URL = "http://127.0.0.1:5173/#local-models";
const SUCCESS_ENDPOINT = "http://127.0.0.1:18884/v1";
const FAILURE_ENDPOINT = "http://127.0.0.1:18885/v1";
const DEBUG_PORT = 19087;
const DOM_WAIT_TIMEOUT_MS = 180_000;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization|prompt|response|key|endpoint)/i;
const ADMIN_EMAIL = "admin@metahub.local";
const DEVELOPER_EMAIL = "developer-prd084@local.invalid";
const SECONDARY_EMAIL = "secondary-prd084@local.invalid";
const NONE_NAME = "Browser None Endpoint";
const BEARER_NAME = "Browser Bearer Endpoint";
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "page-116-create-dialog-desktop-1280x720.png",
  "page-116-local-endpoints-desktop-1280x720.png",
  "page-116-local-endpoints-mobile-390x844.png",
  "page-116-tenant-isolation-mobile-390x844.png",
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
const CREATE_KEYS = Object.freeze([
  "provider_code", "name", "engine_kind", "base_url", "auth_type", "initial_model",
]);
const CREATE_BEARER_KEYS = Object.freeze([...CREATE_KEYS, "bearer_token"]);
const MODEL_KEYS = Object.freeze([
  "internal_model_id", "provider_model_id", "display_name", "capabilities",
  "context_window", "input_price_per_1k", "output_price_per_1k", "currency",
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
  const name = String(error?.name ?? "error").toLowerCase().replace(/[^a-z0-9_.-]/gu, "");
  const location = String(error?.stack ?? "").match(/prd084-browser-acceptance\.mjs:(\d+):\d+/u);
  return new BrowserAcceptanceFailure(stage, `${name || "error"}_l${location?.[1] ?? "0"}`);
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
  const secret = (prefix, bytes = 36) => `${prefix}${randomBytes(bytes).toString("base64url")}`;
  return Object.freeze({
    adminPassword: secret("Prd084-Admin-Aa1-"),
    rolePassword: secret("Prd084-Role-Aa1-"),
    stopToken: secret("Prd084-Stop-"),
    bearerCredential: secret("Prd084-Bearer-"),
    driverOwner: randomBytes(32).toString("hex"),
  });
}


function harnessEnvironment(privateConfig, driverDirectory, host = process.env) {
  const allowed = /^(?:PATH|PATHEXT|SYSTEMROOT|WINDIR|COMSPEC|OS|NUMBER_OF_PROCESSORS|PROCESSOR_ARCHITECTURE|PROGRAMDATA|PROGRAMFILES(?:\(X86\))?|COMMONPROGRAMFILES(?:\(X86\))?)$/i;
  const environment = {};
  for (const [name, value] of Object.entries(host)) {
    if (allowed.test(name) && typeof value === "string") environment[name] = value;
  }
  return {
    ...environment,
    PYTHONDONTWRITEBYTECODE: "1",
    TEMP: driverDirectory,
    TMP: driverDirectory,
    PRD084_BROWSER_ADMIN_PASSWORD: privateConfig.adminPassword,
    PRD084_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD084_BROWSER_STOP_TOKEN: privateConfig.stopToken,
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
  const privateValues = [
    privateConfig.adminPassword,
    privateConfig.rolePassword,
    privateConfig.stopToken,
    privateConfig.bearerCredential,
    privateConfig.driverOwner,
    SUCCESS_ENDPOINT,
    FAILURE_ENDPOINT,
  ];
  return {
    privateValues,
    secrets: privateValues,
    browserTokens: new Set(),
    externalRequests: 0,
    forbiddenQueries: 0,
    consoleErrors: 0,
    pageErrors: 0,
    processPrivacyViolations: 0,
    processPrivacyViolationCount: 0,
    edgeProcessErrorCount: 0,
    requestMethods: new Map(),
    requestCategories: new Map(),
    loginStatuses: [],
    logoutStatuses: [],
    createStatuses: [],
    testStatuses: [],
    listSuccesses: 0,
    noStoreResponses: true,
    noStoreObserved: 0,
    createContracts: [],
    testContractCount: 0,
    requestContractsExact: true,
    pauseExpectation: null,
    pausedResponse: null,
  };
}


function inspectProcessChunk(chunk, tracker) {
  const text = String(chunk ?? "");
  const hits = tracker.privateValues.filter((value) => value && text.includes(value));
  if (hits.length) {
    tracker.processPrivacyViolations += hits.length;
    tracker.processPrivacyViolationCount += 1;
  }
}


function startHarness(runId, privateConfig, driverDirectory, tracker) {
  const child = spawn(PYTHON, [HARNESS, "--run-id", runId], {
    cwd: ROOT,
    env: harnessEnvironment(privateConfig, driverDirectory),
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  let errorOutput = "";
  let readySettled = false;
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const timer = setTimeout(() => {
    if (!readySettled) {
      readySettled = true;
      rejectReady(new BrowserAcceptanceFailure("harness.ready", "timeout"));
    }
  }, 180_000);
  child.stdout.on("data", (chunk) => {
    inspectProcessChunk(chunk, tracker);
    output += String(chunk);
    for (const line of output.split(/\r?\n/u)) {
      if (!line.trim().startsWith("{")) continue;
      try {
        const value = JSON.parse(line);
        if (!readySettled && value.status === "ready" && value.all_108_prds_complete === false) {
          readySettled = true;
          clearTimeout(timer);
          resolveReady(value);
        }
      } catch {}
    }
  });
  child.stderr.on("data", (chunk) => {
    inspectProcessChunk(chunk, tracker);
    errorOutput += String(chunk);
  });
  const closed = new Promise((resolve) => child.once("close", (code) => {
    clearTimeout(timer);
    if (!readySettled) {
      readySettled = true;
      const coordinate = errorOutput.match(
        /PRD-084 browser harness failed: ([a-z][a-z0-9_.-]*):([a-z][a-z0-9_.-]*)/u,
      );
      rejectReady(new BrowserAcceptanceFailure(
        coordinate?.[1] ?? "harness.ready",
        coordinate?.[2] ?? "exited",
      ));
    }
    resolve({ code, output, errorOutput });
  }));
  return { child, ready, closed };
}


async function postHarness(pathname, privateConfig, body = {}) {
  return fetch(`${HARNESS_ORIGIN}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Harness-Token": privateConfig.stopToken },
    body: JSON.stringify(body),
    redirect: "error",
  });
}


async function waitForHarnessClose(harness, timeoutMs = 180_000) {
  let timer;
  const result = await Promise.race([
    harness.closed,
    new Promise((resolve) => { timer = setTimeout(() => resolve(null), timeoutMs); }),
  ]);
  clearTimeout(timer);
  if (!result || result.code !== 0) {
    const coordinate = String(result?.errorOutput ?? "").match(
      /PRD-084 browser harness failed: ([a-z][a-z0-9_.-]*):([a-z][a-z0-9_.-]*)/u,
    );
    throw new BrowserAcceptanceFailure(
      coordinate?.[1] ?? "harness.close",
      coordinate?.[2] ?? "status",
    );
  }
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
  if (target.pathname === "/auth/me" && verb === "GET") return "auth_me";
  if (target.pathname === "/local-endpoints" && verb === "GET") return "endpoint_list";
  if (target.pathname === "/local-endpoints" && verb === "POST") return "endpoint_create";
  if (/^\/local-endpoints\/lme_[0-9a-f]{32}\/test$/.test(target.pathname) && verb === "POST") return "endpoint_test";
  if (target.pathname === "/console/navigation" && verb === "GET") return "console_navigation";
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
  if (category === "endpoint_test") {
    tracker.testContractCount += 1;
    tracker.requestContractsExact &&= String(request?.postData ?? "") === "{}";
    return;
  }
  if (category !== "endpoint_create") return;
  let payload = null;
  try { payload = JSON.parse(String(request?.postData ?? "")); } catch {}
  const authType = payload?.auth_type;
  const expected = authType === "bearer" ? CREATE_BEARER_KEYS : CREATE_KEYS;
  const exact = exactKeys(payload, expected)
    && exactKeys(payload?.initial_model, MODEL_KEYS)
    && ["none", "bearer"].includes(authType)
    && ["vllm", "ollama", "openai_compatible"].includes(payload?.engine_kind)
    && payload?.initial_model?.currency === "CNY"
    && payload?.initial_model?.capabilities?.chat === true
    && !Object.hasOwn(payload, "tenant_id")
    && /^req_[0-9a-f]{32}$/.test(headerValue(request?.headers, "X-Request-ID"))
    && (authType === "none" ? !Object.hasOwn(payload, "bearer_token") : typeof payload?.bearer_token === "string");
  tracker.requestContractsExact &&= exact;
  tracker.createContracts.push(authType);
}


function recordResponseStatus(tracker, event) {
  const requestId = String(event?.requestId ?? "");
  const method = tracker.requestMethods.get(requestId) ?? "GET";
  const category = tracker.requestCategories.get(requestId) ?? classifyRequest(event?.response?.url, method);
  const status = Number(event?.response?.status ?? 0);
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category === "endpoint_create") tracker.createStatuses.push(status);
  if (category === "endpoint_test") tracker.testStatuses.push(status);
  if (category === "endpoint_list" && status === 200) tracker.listSuccesses += 1;
  if (["endpoint_list", "endpoint_create", "endpoint_test"].includes(category)) {
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
    { urlPattern: `${CONTROL_ORIGIN}/local-endpoints*`, requestStage: "Response" },
    { urlPattern: "http://127.0.0.1:8001/local-endpoints*", requestStage: "Response" },
  ] }, sessionId);
}


async function waitForDom(client, sessionId, expression, stage) {
  return waitForDomBase(client, sessionId, expression, stage, DOM_WAIT_TIMEOUT_MS);
}


async function settle(client, sessionId, stage) {
  return evaluate(client, sessionId, "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))", stage);
}


async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#local-models` }, sessionId),
    "browser.console.load",
    60_000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


async function login(client, sessionId, tracker, tenantSlug, email, password, stage) {
  const before = tracker.loginStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#login-form');
    if (!form) return false;
    form.elements.namedItem('tenantSlug').value = ${JSON.stringify(tenantSlug)};
    form.elements.namedItem('email').value = ${JSON.stringify(email)};
    form.elements.namedItem('password').value = ${JSON.stringify(password)};
    for (const field of form.querySelectorAll('input')) field.dispatchEvent(new Event('input', { bubbles: true }));
    form.requestSubmit(); return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  try {
    await waitForDom(client, sessionId, "document.querySelector('#logout-button')", `${stage}.dom`);
  } catch {
    const state = await evaluate(client, sessionId, `(() => ({
      hash:location.hash==='#local-models',login:Boolean(document.querySelector('#login-form')),
      logout:Boolean(document.querySelector('#logout-button')),shell:Boolean(document.querySelector('.console-shell')),
      alert:Boolean(document.querySelector('[role=alert]')),
    }))()`, `${stage}.diagnostic`);
    throw new BrowserAcceptanceFailure(`${stage}.dom`, [
      "timeout", `h${Number(state.hash)}`, `i${Number(state.login)}`, `o${Number(state.logout)}`,
      `s${Number(state.shell)}`, `a${Number(state.alert)}`,
    ].join("."));
  }
  await waitForTrackerCount(() => tracker.loginStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.loginStatuses.at(-1) === 200, stage, "status");
}


async function logout(client, sessionId, tracker, stage) {
  const before = tracker.logoutStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('#logout-button');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `${stage}.form`);
  await waitForTrackerCount(() => tracker.logoutStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.logoutStatuses.at(-1) === 200, stage, "status");
}


async function openLocalPage(client, sessionId, stage) {
  try {
    await waitForDom(
      client,
      sessionId,
      "document.querySelector('[data-local-endpoint-root]') || document.querySelector('[data-console-nav=\"local-models\"]')",
      `${stage}.shell`,
    );
  } catch {
    const state = await evaluate(client, sessionId, `(() => ({
      hash:location.hash==='#local-models',login:Boolean(document.querySelector('#login-form')),
      logout:Boolean(document.querySelector('#logout-button')),shell:Boolean(document.querySelector('.console-shell')),
      root:Boolean(document.querySelector('[data-local-endpoint-root]')),
      nav:Boolean(document.querySelector('[data-console-nav="local-models"]')),
    }))()`, `${stage}.diagnostic`);
    throw new BrowserAcceptanceFailure(`${stage}.shell`, [
      "timeout", `h${Number(state.hash)}`, `i${Number(state.login)}`, `o${Number(state.logout)}`,
      `s${Number(state.shell)}`, `r${Number(state.root)}`, `n${Number(state.nav)}`,
    ].join("."));
  }
  const hasRoot = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-local-endpoint-root]'))", `${stage}.current`);
  if (!hasRoot) {
    const clicked = await evaluate(client, sessionId, `(() => {
      const button=document.querySelector('[data-console-nav="local-models"]');
      if (!button || button.disabled) return false; button.click(); return true;
    })()`, `${stage}.navigate`);
    requireCondition(clicked === true, stage, "navigation");
  }
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-local-endpoint-root]') && !document.querySelector('[data-local-endpoint-root] .local-endpoint-empty[role=status]')",
    `${stage}.ready`,
  );
}


async function openCreateDialog(client, sessionId, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-local-endpoint-open-create]');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`, stage);
  requireCondition(opened === true, stage, "button");
  await waitForDom(client, sessionId, "document.querySelector('dialog[data-local-endpoint-dialog][open]')", `${stage}.ready`);
}


async function closeCreateDialog(client, sessionId, stage) {
  const closed = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('dialog[data-local-endpoint-dialog][open] [data-local-endpoint-close]');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`, stage);
  requireCondition(closed === true, stage, "button");
  await waitForDom(client, sessionId, "!document.querySelector('dialog[data-local-endpoint-dialog]')", `${stage}.closed`);
}


async function createEndpoint(client, sessionId, tracker, values, stage) {
  await openCreateDialog(client, sessionId, `${stage}.dialog`);
  const before = tracker.createStatuses.length;
  const cleared = await evaluate(client, sessionId, `(() => {
    const form=document.querySelector('[data-local-endpoint-create-form]');
    if (!form) return false;
    const values=${JSON.stringify(values)};
    const assign=(name,value) => {
      const field=form.elements.namedItem(name); if (!field) return false;
      field.value=String(value); field.dispatchEvent(new Event('input',{bubbles:true}));
      field.dispatchEvent(new Event('change',{bubbles:true})); return true;
    };
    if (!assign('provider_code',values.providerCode) || !assign('name',values.name)
      || !assign('base_url',values.baseUrl) || !assign('internal_model_id',values.internalModel)
      || !assign('provider_model_id',values.providerModel) || !assign('display_name',values.displayName)
      || !assign('context_window','32768') || !assign('input_price_per_1k','0.1')
      || !assign('output_price_per_1k','0.2')) return false;
    const engine=form.querySelector('input[name=engine_kind][value="'+values.engine+'"]');
    const auth=form.querySelector('input[name=auth_type][value="'+values.authType+'"]');
    if (!engine || !auth) return false;
    engine.checked=true; engine.dispatchEvent(new Event('change',{bubbles:true}));
    auth.checked=true; auth.dispatchEvent(new Event('change',{bubbles:true}));
    if (values.authType === 'bearer' && !assign('bearer_token',values.bearerCredential)) return false;
    form.requestSubmit();
    const next=document.querySelector('[data-local-endpoint-create-form]');
    const endpoint=next?.elements?.namedItem?.('base_url');
    const credential=next?.elements?.namedItem?.('bearer_token');
    return (!endpoint || endpoint.value === '') && (!credential || credential.value === '');
  })()`, stage);
  requireCondition(cleared === true, stage, "sensitive_clear");
  await waitForTrackerCount(() => tracker.createStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.createStatuses.at(-1) === 201, stage, "status");
  await waitForDom(
    client,
    sessionId,
    `!document.querySelector('dialog[data-local-endpoint-dialog]') && [...document.querySelectorAll('.local-endpoint-table tbody tr')].some((row)=>row.textContent.includes(${JSON.stringify(values.name)}))`,
    `${stage}.row`,
  );
  return true;
}


async function testEndpoint(client, sessionId, tracker, name, expectedTone, stage) {
  const before = tracker.testStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const row=[...document.querySelectorAll('.local-endpoint-table tbody tr')]
      .find((item)=>item.textContent.includes(${JSON.stringify(name)}));
    const button=row?.querySelector('[data-local-endpoint-test]');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForTrackerCount(() => tracker.testStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.testStatuses.at(-1) === 200, stage, "status");
  await waitForDom(
    client,
    sessionId,
    `[...document.querySelectorAll('.local-endpoint-table tbody tr')].some((row)=>row.textContent.includes(${JSON.stringify(name)}) && row.querySelector('.local-endpoint-status.${expectedTone}'))`,
    `${stage}.result`,
  );
  return true;
}


async function readLayoutMetrics(client, sessionId, focusSelector) {
  return evaluate(client, sessionId, `(() => {
    const root=document.querySelector('[data-local-endpoint-root]');
    const scope=root?.querySelector('dialog[open]') || root;
    const focus=document.querySelector(${JSON.stringify(focusSelector)});
    const shown=(item)=>{ if(!item)return false; const style=getComputedStyle(item); const rect=item.getBoundingClientRect();
      return style.display!=='none' && style.visibility!=='hidden' && rect.width>0 && rect.height>0; };
    const controls=[...(scope?.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])')||[])].filter(shown);
    let overlaps=0;
    for(let first=0;first<controls.length;first+=1){ const a=controls[first].getBoundingClientRect();
      for(let second=first+1;second<controls.length;second+=1){
        if(controls[first].contains(controls[second])||controls[second].contains(controls[first]))continue;
        const b=controls[second].getBoundingClientRect();
        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>2 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>2)overlaps+=1;
      }}
    const textOverflow=[...(scope?.querySelectorAll('button,label,span,strong,small,dt,dd,p,h2,h3,h4')||[])]
      .filter((item)=>shown(item) && !item.closest('.local-endpoint-table-scroll'))
      .some((item)=>!['auto','scroll'].includes(getComputedStyle(item).overflowX) && item.scrollWidth>item.clientWidth+2);
    const scroll=root?.querySelector('.local-endpoint-table-scroll');
    const scrollRect=scroll?.getBoundingClientRect();
    const focusRect=focus?.getBoundingClientRect();
    return {
      width:innerWidth,height:innerHeight,rootVisible:shown(root),focusVisible:shown(focus)
        && focusRect.bottom>0 && focusRect.top<innerHeight && focusRect.right>0 && focusRect.left<innerWidth,
      horizontalOverflow:document.documentElement.scrollWidth>innerWidth+1 || document.body.scrollWidth>innerWidth+1,
      textOverflow,overlapCount:overlaps,
      tableScrollBounded:!scroll || (scrollRect.left>=-1 && scrollRect.right<=innerWidth+1 && scroll.clientWidth<=innerWidth),
    };
  })()`, "browser.layout.read");
}


async function captureScreenshot(client, sessionId, staging, filename, viewport, focusSelector, tracker, stage) {
  await setViewport(client, sessionId, viewport);
  requireCondition(await evaluate(client, sessionId, `(() => {
    const focus=document.querySelector(${JSON.stringify(focusSelector)});
    if(!focus)return false; focus.scrollIntoView({block:'center',inline:'nearest'}); return true;
  })()`, `${stage}.focus`) === true, stage, "focus");
  await settle(client, sessionId, `${stage}.settle`);
  const privacy = await evaluate(client, sessionId, `(() => {
    const needles=${JSON.stringify(tracker.privateValues)};
    const root=document.querySelector('[data-local-endpoint-root]');
    const values=[root?.textContent||'',...[...(root?.querySelectorAll('input,textarea,select')||[])].map((item)=>String(item.value||'')),
      ...[...(root?.querySelectorAll('*')||[])].flatMap((item)=>[...item.attributes].map((attribute)=>attribute.value))];
    return needles.every((needle)=>!values.some((value)=>value.includes(needle)));
  })()`, `${stage}.privacy`);
  requireCondition(privacy === true, stage, "privacy");
  const layout = await readLayoutMetrics(client, sessionId, focusSelector);
  requireCondition(layout.width === viewport.width && layout.height === viewport.height
    && layout.rootVisible && layout.focusVisible && !layout.horizontalOverflow
    && !layout.textOverflow && layout.overlapCount === 0 && layout.tableScrollBounded,
  stage, "layout");
  await evaluate(client, sessionId, `(() => { const item=document.querySelector('.user-menu'); if(!item)return false;
    item.dataset.prd084Mask='true'; item.style.visibility='hidden'; return true; })()`, `${stage}.mask`);
  let result;
  try {
    result = await client.send("Page.captureScreenshot", { format: "png", fromSurface: true, captureBeyondViewport: false }, sessionId, 30_000);
  } finally {
    await evaluate(client, sessionId, `(() => { const item=document.querySelector('[data-prd084-mask=true]'); if(!item)return false;
      item.style.visibility=''; delete item.dataset.prd084Mask; return true; })()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({ layout, nonblank: true, noSensitiveDom: true });
}


async function exerciseAdmin(client, sessionId, tracker, staging, privateConfig) {
  await login(client, sessionId, tracker, "default", ADMIN_EMAIL, privateConfig.adminPassword, "browser.login.admin");
  await openLocalPage(client, sessionId, "browser.admin.local");
  await openCreateDialog(client, sessionId, "browser.admin.dialog_preview");
  const nativeDialog = await evaluate(client, sessionId, `(() => {
    const dialog=document.querySelector('dialog[data-local-endpoint-dialog][open]');
    return dialog instanceof HTMLDialogElement && dialog.open
      && dialog.querySelector('input[name=base_url]')?.value === ''
      && dialog.querySelector('input[name=bearer_token]')?.value === '';
  })()`, "browser.admin.dialog_contract");
  requireCondition(nativeDialog === true, "browser.admin.dialog_contract", "native");
  const dialogDesktop = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[0], VIEWPORTS.desktop,
    "dialog[data-local-endpoint-dialog]", tracker, "browser.screenshot.dialog_desktop",
  );
  await closeCreateDialog(client, sessionId, "browser.admin.dialog_close");
  const noneCleared = await createEndpoint(client, sessionId, tracker, {
    providerCode: "browser_none", name: NONE_NAME, baseUrl: SUCCESS_ENDPOINT,
    engine: "vllm", authType: "none", bearerCredential: "",
    internalModel: "browser-none", providerModel: "browser-chat", displayName: "Browser None Model",
  }, "browser.admin.create_none");
  const noneSuccess = await testEndpoint(client, sessionId, tracker, NONE_NAME, "tone-healthy", "browser.admin.test_none");
  const bearerCleared = await createEndpoint(client, sessionId, tracker, {
    providerCode: "browser_bearer", name: BEARER_NAME, baseUrl: FAILURE_ENDPOINT,
    engine: "openai_compatible", authType: "bearer", bearerCredential: privateConfig.bearerCredential,
    internalModel: "browser-bearer", providerModel: "browser-private", displayName: "Browser Bearer Model",
  }, "browser.admin.create_bearer");
  const bearerFailure = await testEndpoint(client, sessionId, tracker, BEARER_NAME, "tone-unhealthy", "browser.admin.test_bearer");
  const summary = await evaluate(client, sessionId, `(() => {
    const rows=[...document.querySelectorAll('.local-endpoint-table tbody tr')];
    const text=document.querySelector('[data-local-endpoint-root]')?.textContent||'';
    return { rows:rows.length, none:rows.some((row)=>row.textContent.includes(${JSON.stringify(NONE_NAME)}) && row.querySelector('.tone-healthy')),
      bearer:rows.some((row)=>row.textContent.includes(${JSON.stringify(BEARER_NAME)}) && row.querySelector('.tone-unhealthy')),
      credentialConfigured:text.includes('Bearer'), discovered:text.includes('2'),
      tokenAbsent:!document.querySelector('input[name=bearer_token]'), urlAbsent:!document.querySelector('input[name=base_url]') };
  })()`, "browser.admin.summary");
  requireCondition(summary.rows === 2 && summary.none && summary.bearer && summary.credentialConfigured
    && summary.discovered && summary.tokenAbsent && summary.urlAbsent, "browser.admin.summary", "contract");
  const listDesktop = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[1], VIEWPORTS.desktop,
    ".local-endpoint-table-scroll", tracker, "browser.screenshot.list_desktop",
  );
  const listMobile = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[2], VIEWPORTS.mobile,
    ".local-endpoint-table-scroll", tracker, "browser.screenshot.list_mobile",
  );
  await logout(client, sessionId, tracker, "browser.logout.admin");
  return Object.freeze({
    dialogDesktop, listDesktop, listMobile, nativeDialog,
    noneSuccess, bearerFailure, tokenCleared: bearerCleared, urlCleared: noneCleared && bearerCleared,
    endpointRows: summary.rows,
  });
}


async function exerciseDeveloperAndDrift(client, sessionId, tracker, privateConfig) {
  await login(client, sessionId, tracker, "default", DEVELOPER_EMAIL, privateConfig.rolePassword, "browser.login.developer");
  const navigationVisible = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-console-nav=local-models]'))", "browser.developer.navigation");
  await openLocalPage(client, sessionId, "browser.developer.local");
  const readTestOnly = await evaluate(client, sessionId, `(() => {
    const rows=document.querySelectorAll('.local-endpoint-table tbody tr');
    return rows.length===2 && !document.querySelector('[data-local-endpoint-open-create]')
      && document.querySelectorAll('[data-local-endpoint-test]').length===2;
  })()`, "browser.developer.permissions");
  requireCondition(navigationVisible && readTestOnly, "browser.developer", "permissions");
  await testEndpoint(client, sessionId, tracker, NONE_NAME, "tone-healthy", "browser.developer.test");
  const paused = pauseNextResponse(tracker, "endpoint_list", 200, "browser.revoke.pause");
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-local-endpoint-refresh]');
    if(!button||button.disabled)return false; button.click(); return true;
  })()`, "browser.revoke.refresh");
  requireCondition(refreshed === true, "browser.revoke.refresh", "button");
  await paused;
  requireCondition((await postHarness("/revoke-local-read", privateConfig, {})).status === 200, "browser.revoke", "status");
  const navigated = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-console-nav=overview]');
    if(!button||button.disabled)return false; button.click(); return true;
  })()`, "browser.revoke.navigate");
  requireCondition(navigated === true, "browser.revoke.navigate", "button");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-local-endpoint-root], .local-models-panel [data-local-endpoint-test], [data-console-nav=local-models]')",
    "browser.revoke.cleared",
  );
  const before = await evaluate(client, sessionId, `(() => {
    const body=document.body.textContent||'';
    return { root:!document.querySelector('[data-local-endpoint-root]'), navigation:!document.querySelector('[data-console-nav=local-models]'),
      endpoints:!body.includes(${JSON.stringify(NONE_NAME)})&&!body.includes(${JSON.stringify(BEARER_NAME)}) };
  })()`, "browser.revoke.before_release");
  await releasePausedResponse(client, tracker, "browser.revoke.release", { allowClosed: true });
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const lateDiscarded = await evaluate(client, sessionId, `(() => {
    const body=document.body.textContent||'';
    return !document.querySelector('[data-local-endpoint-root]')&&!body.includes(${JSON.stringify(NONE_NAME)})&&!body.includes(${JSON.stringify(BEARER_NAME)});
  })()`, "browser.revoke.late");
  await logout(client, sessionId, tracker, "browser.logout.developer");
  return Object.freeze({ navigationVisible, readTestOnly, ...before, lateDiscarded });
}


async function exerciseSecondary(client, sessionId, tracker, staging, privateConfig) {
  await login(client, sessionId, tracker, "prd084-secondary", SECONDARY_EMAIL, privateConfig.adminPassword, "browser.login.secondary");
  const navigationVisible = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-console-nav=local-models]'))", "browser.secondary.navigation");
  await openLocalPage(client, sessionId, "browser.secondary.local");
  const isolation = await evaluate(client, sessionId, `(() => {
    const root=document.querySelector('[data-local-endpoint-root]'); const text=root?.textContent||'';
    return { empty:!root?.querySelector('.local-endpoint-table tbody tr') && Boolean(root?.querySelector('.local-endpoint-empty')),
      previous:!text.includes(${JSON.stringify(NONE_NAME)})&&!text.includes(${JSON.stringify(BEARER_NAME)}) };
  })()`, "browser.secondary.isolation");
  requireCondition(navigationVisible && isolation.empty && isolation.previous, "browser.secondary", "isolation");
  const screenshot = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[3], VIEWPORTS.mobile,
    ".local-endpoint-empty", tracker, "browser.screenshot.secondary_mobile",
  );
  await logout(client, sessionId, tracker, "browser.logout.secondary");
  const storageCleared = await evaluate(client, sessionId, "localStorage.length===0 && sessionStorage.length===0", "browser.logout.storage");
  return Object.freeze({ navigationVisible, ...isolation, screenshot, storageCleared });
}


async function verifyFinalPrivacy(client, sessionId, tracker) {
  const result = await evaluate(client, sessionId, `(async () => {
    const needles=${JSON.stringify(tracker.privateValues)};
    const values=[document.documentElement.textContent||'',...[...document.querySelectorAll('input,textarea,select')].map((item)=>String(item.value||'')),
      ...Object.entries(localStorage).flat(),...Object.entries(sessionStorage).flat(),location.href];
    return { hits:needles.filter((needle)=>values.some((value)=>String(value).includes(needle))).length,
      caches:typeof caches==='undefined'?0:(await caches.keys()).length,
      databases:typeof indexedDB.databases!=='function'?0:(await indexedDB.databases()).length,
      login:Boolean(document.querySelector('#login-form')) };
  })()`, "browser.privacy");
  requireCondition(result.hits===0 && result.caches===0 && result.databases===0 && result.login, "browser.privacy", "persistent");
  return true;
}


function requestContractsExact(tracker) {
  return tracker.requestContractsExact
    && JSON.stringify(tracker.createContracts) === JSON.stringify(["none", "bearer"])
    && tracker.testContractCount === 3;
}


function buildCompletionFacts({ admin, developer, secondary, registration, tracker }) {
  return {
    status: "passed",
    desktop: {
      width: 1280, height: 720, nonblank: admin.dialogDesktop.nonblank && admin.listDesktop.nonblank,
      page_116: true, endpoint_rows: admin.endpointRows, none_success: admin.noneSuccess,
      bearer_failure: admin.bearerFailure, native_dialog: admin.nativeDialog,
      token_cleared: admin.tokenCleared, url_cleared: admin.urlCleared,
      horizontal_overflow_free: admin.dialogDesktop.layout.horizontalOverflow === false
        && admin.listDesktop.layout.horizontalOverflow === false,
      table_scroll_bounded: admin.listDesktop.layout.tableScrollBounded,
      text_overflow_free: admin.dialogDesktop.layout.textOverflow === false
        && admin.listDesktop.layout.textOverflow === false,
      overlap_count: Math.max(admin.dialogDesktop.layout.overlapCount, admin.listDesktop.layout.overlapCount),
      no_sensitive_dom: admin.dialogDesktop.noSensitiveDom && admin.listDesktop.noSensitiveDom,
    },
    mobile: {
      width: 390, height: 844, nonblank: admin.listMobile.nonblank && secondary.screenshot.nonblank,
      page_116: true, endpoint_rows: admin.endpointRows, bearer_failure: admin.bearerFailure,
      tenant_isolation_empty: secondary.empty,
      horizontal_overflow_free: admin.listMobile.layout.horizontalOverflow === false
        && secondary.screenshot.layout.horizontalOverflow === false,
      table_scroll_bounded: admin.listMobile.layout.tableScrollBounded,
      text_overflow_free: admin.listMobile.layout.textOverflow === false
        && secondary.screenshot.layout.textOverflow === false,
      overlap_count: Math.max(admin.listMobile.layout.overlapCount, secondary.screenshot.layout.overlapCount),
      no_sensitive_dom: admin.listMobile.noSensitiveDom && secondary.screenshot.noSensitiveDom,
    },
    lifecycle: {
      backend_developer_manage_denied: true, backend_secondary_empty: true,
      developer_navigation_visible: developer.navigationVisible,
      developer_read_test_only: developer.readTestOnly,
      permission_drift_root_cleared: developer.root,
      permission_drift_navigation_cleared: developer.navigation,
      permission_drift_endpoint_dom_cleared: developer.endpoints,
      late_response_discarded: developer.lateDiscarded,
      secondary_navigation_visible: secondary.navigationVisible,
      secondary_empty: secondary.empty,
      previous_tenant_dom_cleared: secondary.previous,
      registered_private_values: registration.status === 200,
      logout_storage_cleared: secondary.storageCleared,
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
      no_store_responses: tracker.noStoreResponses && tracker.noStoreObserved >= 8,
      request_contracts_exact: requestContractsExact(tracker),
    },
    screenshots: SCREENSHOTS,
    all_108_prds_complete: false,
  };
}


function validateCompletionFacts(value) {
  requireCondition(value?.status === "passed" && value?.all_108_prds_complete === false, "browser.complete", "status");
  requireCondition(value.desktop?.width === 1280 && value.desktop?.height === 720
    && value.desktop?.endpoint_rows === 2 && value.desktop?.overlap_count === 0, "browser.desktop", "layout");
  requireCondition(value.mobile?.width === 390 && value.mobile?.height === 844
    && value.mobile?.endpoint_rows === 2 && value.mobile?.overlap_count === 0, "browser.mobile", "layout");
  for (const section of [value.desktop, value.mobile, value.lifecycle]) {
    requireCondition(Object.entries(section).every(([name, item]) => ["width", "height", "overlap_count", "endpoint_rows"].includes(name) || item === true), "browser.complete", "facts");
  }
  const networkValid = value.network?.external_requests === 0 && value.network?.forbidden_queries === 0
    && value.network?.console_errors === 0 && value.network?.page_errors === 0
    && JSON.stringify(value.network?.login_statuses) === JSON.stringify([200, 200, 200])
    && JSON.stringify(value.network?.logout_statuses) === JSON.stringify([200, 200, 200])
    && JSON.stringify(value.network?.create_statuses) === JSON.stringify([201, 201])
    && JSON.stringify(value.network?.test_statuses) === JSON.stringify([200, 200, 200])
    && value.network?.list_successes >= 3 && value.network?.no_store_responses === true
    && value.network?.request_contracts_exact === true;
  const networkCoordinate = [
    "facts",
    `e${Number(value.network?.external_requests === 0)}`,
    `q${Number(value.network?.forbidden_queries === 0)}`,
    `c${Number(value.network?.console_errors === 0)}`,
    `p${Number(value.network?.page_errors === 0)}`,
    `i${Number(JSON.stringify(value.network?.login_statuses) === JSON.stringify([200, 200, 200]))}`,
    `o${Number(JSON.stringify(value.network?.logout_statuses) === JSON.stringify([200, 200, 200]))}`,
    `k${Number(JSON.stringify(value.network?.create_statuses) === JSON.stringify([201, 201]))}`,
    `t${Number(JSON.stringify(value.network?.test_statuses) === JSON.stringify([200, 200, 200]))}`,
    `l${Math.min(99, Number(value.network?.list_successes ?? 0))}`,
    `n${Number(value.network?.no_store_responses === true)}`,
    `r${Number(value.network?.request_contracts_exact === true)}`,
  ].join(".");
  requireCondition(networkValid, "browser.network", networkCoordinate);
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
      const original=window.setInterval.bind(window); window.setInterval=(callback,delay,...args)=>Number(delay)===15000?0:original(callback,delay,...args);
    })();` }, pageSession.sessionId);
    await loadConsole(pageSession.client, pageSession.sessionId);
    const admin = await exerciseAdmin(pageSession.client, pageSession.sessionId, tracker, staging, privateConfig);
    const developer = await exerciseDeveloperAndDrift(pageSession.client, pageSession.sessionId, tracker, privateConfig);
    const secondary = await exerciseSecondary(pageSession.client, pageSession.sessionId, tracker, staging, privateConfig);
    await verifyFinalPrivacy(pageSession.client, pageSession.sessionId, tracker);
    requireCondition(tracker.browserTokens.size === 3 && tracker.processPrivacyViolations === 0
      && tracker.processPrivacyViolationCount === 0 && tracker.edgeProcessErrorCount === 0,
    "browser.process", "privacy_or_error");
    const registration = await postHarness("/register-private", privateConfig, {
      values: [...new Set([
        privateConfig.driverOwner,
        privateConfig.bearerCredential,
        SUCCESS_ENDPOINT,
        FAILURE_ENDPOINT,
        ...tracker.browserTokens,
      ])],
    });
    requireCondition(registration.status === 200, "harness.private", "status");
    const facts = buildCompletionFacts({ admin, developer, secondary, registration, tracker });
    validateCompletionFacts(facts);
    await closeBrowser(browserClient, pageSession, browserChild);
    browserClient = null; pageSession = null; browserChild = null;
    await removeDriverDirectory(driverDirectory, privateConfig.driverOwner);
    requireCondition((await postHarness("/complete", privateConfig, facts)).status === 200, "harness.complete", "status");
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-084", screenshot_count: 4 });
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
  CREATE_BEARER_KEYS,
  CREATE_KEYS,
  DEBUG_PORT,
  HARNESS_ORIGIN,
  MODEL_KEYS,
  SCREENSHOTS,
  VIEWPORTS,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  createDriverDirectory,
  parseArguments,
  requestContractsExact,
  safeRunId,
  validateCompletionFacts,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
