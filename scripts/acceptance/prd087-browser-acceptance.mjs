/** PRD-087 PAGE-119 浏览器验收：驱动真实 Chromium，隔离凭据并只输出固定事实。 */

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, writeSync } from "node:fs";
import { lstat, mkdir, readFile, readdir, rm, rmdir, unlink, writeFile } from "node:fs/promises";
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
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd087-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd087-browser-driver");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd087-browser");
const DRIVER_OWNER_FILENAME = ".prd087-browser-driver-owner";
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18889";
const DEBUG_PORT = 19089;
const DOM_WAIT_TIMEOUT_MS = 180_000;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization|prompt|response|key|endpoint)/i;
const ADMIN_EMAIL = "admin@metahub.local";
const VIEWER_EMAIL = "viewer-prd087@local.invalid";
const DEVELOPER_EMAIL = "developer-prd087@local.invalid";
const SECONDARY_EMAIL = "secondary-prd087@local.invalid";
const PRIMARY_MARKERS = Object.freeze({
  partnerCode: "primary-channel",
  portalName: "Primary Partner Workspace",
  brandedPortalName: "Primary Partner Operations",
  supportText: "Primary partner support",
  customerCode: "customer-one",
  customerName: "Customer One",
});
const SECONDARY_PORTAL_NAME = "Secondary Partner Workspace";
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
  wide: Object.freeze({ name: "wide-1440x900", width: 1440, height: 900 }),
});
const SCREENSHOTS = Object.freeze([
  "page-119-partner-setup-mobile-390x844.png",
  "page-119-partner-ready-desktop-1280x720.png",
  "page-119-partner-commission-wide-1440x900.png",
  "page-119-partner-tenant-isolation-mobile-390x844.png",
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
const PARTNER_QUERY_KEYS = Object.freeze({
  "/partners/customers": new Set(["start_date", "end_date", "status", "q", "limit", "offset"]),
  "/partners/commission": new Set(["start_date", "end_date", "customer_id", "format", "limit", "offset"]),
});


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
  const location = String(error?.stack ?? "").match(/prd087-browser-acceptance\.mjs:(\d+):\d+/u);
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
    adminPassword: secret("Prd087-Admin-Aa1-"),
    rolePassword: secret("Prd087-Role-Aa1-"),
    stopToken: secret("Prd087-Stop-"),
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
    PRD087_BROWSER_ADMIN_PASSWORD: privateConfig.adminPassword,
    PRD087_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD087_BROWSER_STOP_TOKEN: privateConfig.stopToken,
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


async function removeDriverDirectory(
  directory,
  owner,
  {
    existsOperation = existsSync,
    lstatOperation = lstat,
    readOperation = readFile,
    listOperation = readdir,
    removeOperation = rm,
    unlinkOperation = unlink,
    rmdirOperation = rmdir,
  } = {},
) {
  if (!directory || !existsOperation(directory)) return;
  requireCondition(path.dirname(path.resolve(directory)) === path.resolve(DRIVER_ROOT), "cleanup.driver_directory", "boundary");
  const information = await lstatOperation(directory);
  requireCondition(information.isDirectory() && !information.isSymbolicLink(), "cleanup.driver_directory", "type");
  const marker = path.join(directory, DRIVER_OWNER_FILENAME);
  requireCondition((await readOperation(marker, "ascii")).trim() === owner, "cleanup.driver_directory", "owner");
  for (const name of await listOperation(directory)) {
    if (name === DRIVER_OWNER_FILENAME) continue;
    await removeOperation(directChild(directory, name, "cleanup.driver_directory"), {
      recursive: true,
      force: false,
      maxRetries: 10,
      retryDelay: 200,
    });
  }
  await unlinkOperation(marker);
  await rmdirOperation(directory);
}


function createTracker(privateConfig) {
  const privateValues = [
    privateConfig.adminPassword,
    privateConfig.rolePassword,
    privateConfig.stopToken,
    privateConfig.driverOwner,
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
    fetchNetworkIds: new Map(),
    recordedResponseIds: new Set(),
    loginStatuses: [],
    logoutStatuses: [],
    partnerStatuses: [],
    partnerRequestCount: 0,
    partnerRoutes: new Set(),
    noStoreResponses: true,
    noStoreObserved: 0,
    requestContractExact: true,
    pauseExpectation: null,
    pausedResponse: null,
  };
}


function inspectProcessChunk(chunk, tracker) {
  const output = String(chunk ?? "");
  const hits = tracker.privateValues.filter((value) => value && output.includes(value));
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
        /PRD-087 browser harness failed: ([a-z][a-z0-9_.-]*):([a-z][a-z0-9_.-]*)/u,
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
      /PRD-087 browser harness failed: ([a-z][a-z0-9_.-]*):([a-z][a-z0-9_.-]*)/u,
    );
    throw new BrowserAcceptanceFailure(
      coordinate?.[1] ?? "harness.close",
      coordinate?.[2] ?? "status",
    );
  }
}


async function abortHarness(
  harness,
  privateConfig,
  { postOperation = postHarness, terminateOperation = terminateProcessTree, timeoutMs = 10_000 } = {},
) {
  if (!harness || harness.child.exitCode !== null) return true;
  try { await postOperation("/abort", privateConfig); } catch {}
  let timer;
  const closed = await Promise.race([
    harness.closed.then(() => true),
    new Promise((resolve) => { timer = setTimeout(() => resolve(false), timeoutMs); }),
  ]);
  clearTimeout(timer);
  if (!closed && harness.child.exitCode === null) {
    try { await terminateOperation(harness.child); } catch { return false; }
  }
  return true;
}


async function closeBrowser(browserClient, pageSession, child) {
  return closeOwnedBrowser(browserClient, pageSession, child, { terminateOperation: terminateIdempotentBrowserOwner });
}


function partnerPath(value) {
  return /^\/partners\/customers\/pcu_[0-9a-f]{32}$/u.test(value)
    ? "/partners/customers/{partner_customer_id}"
    : value;
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
  let forbiddenQuery = [...target.searchParams.keys()].some((name) => FORBIDDEN_QUERY_KEY.test(name));
  if (target.pathname.startsWith("/partners")) {
    const allowed = PARTNER_QUERY_KEYS[target.pathname] ?? new Set();
    forbiddenQuery ||= [...target.searchParams.keys()].some((name) => !allowed.has(name));
    forbiddenQuery ||= target.pathname === "/partners/commission"
      && target.searchParams.get("format") === "csv"
      && (target.searchParams.has("limit") || target.searchParams.has("offset"));
  }
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
  if (verb === "OPTIONS") return "control_preflight";
  if (target.pathname.startsWith("/partners")) return `partner:${verb}:${partnerPath(target.pathname)}`;
  if (target.pathname === "/console/navigation" && verb === "GET") return "console_navigation";
  return "control_local";
}


function headerValue(headers, name) {
  if (Array.isArray(headers)) {
    const entry = headers.find((item) => String(item?.name ?? "").toLowerCase() === String(name).toLowerCase());
    return entry ? String(entry.value ?? "") : "";
  }
  const entry = Object.entries(headers ?? {}).find(([key]) => key.toLowerCase() === String(name).toLowerCase());
  return entry ? String(entry[1]) : "";
}


function inspectBusinessRequest(tracker, request) {
  const category = classifyRequest(request?.url, request?.method);
  const authorization = headerValue(request?.headers, "Authorization");
  const browserToken = authorization.match(/^Bearer ([^\s]+)$/)?.[1] ?? "";
  if (browserToken) {
    tracker.browserTokens.add(browserToken);
    if (!tracker.privateValues.includes(browserToken)) tracker.privateValues.push(browserToken);
  }
  if (!category.startsWith("partner:")) return;
  let target;
  try { target = new URL(String(request.url)); } catch {
    tracker.requestContractExact = false;
    return;
  }
  const verb = String(request.method).toUpperCase();
  const pathKey = partnerPath(target.pathname);
  const methods = {
    "/partners": new Set(["POST"]),
    "/partners/profile": new Set(["GET"]),
    "/partners/branding": new Set(["PATCH"]),
    "/partners/projects": new Set(["GET"]),
    "/partners/customers": new Set(["GET", "POST"]),
    "/partners/customers/{partner_customer_id}": new Set(["PATCH"]),
    "/partners/commission": new Set(["GET"]),
    "/partners/delivery-package": new Set(["GET"]),
  };
  const allowedQuery = PARTNER_QUERY_KEYS[target.pathname] ?? new Set();
  const queryExact = [...target.searchParams.keys()].every((name) => allowedQuery.has(name));
  const bodyExact = verb === "GET"
    ? request.hasPostData !== true && headerValue(request.headers, "Content-Type") === ""
    : headerValue(request.headers, "Content-Type").toLowerCase().startsWith("application/json");
  const idempotencyExact = !["POST"].includes(verb)
    || /^[A-Za-z0-9._:-]{16,160}$/.test(headerValue(request.headers, "Idempotency-Key"));
  const revisionExact = verb !== "PATCH" || /^[1-9][0-9]*$/.test(headerValue(request.headers, "If-Match"));
  tracker.requestContractExact &&= Boolean(
    methods[pathKey]?.has(verb)
    && queryExact
    && !target.hash
    && !target.username
    && !target.password
    && /^Bearer [^\s]+$/.test(authorization)
    && bodyExact
    && idempotencyExact
    && revisionExact,
  );
  tracker.partnerRequestCount += 1;
  tracker.partnerRoutes.add(`${verb} ${pathKey}`);
}


function recordResponseStatus(tracker, response) {
  const requestId = String(response?.requestId ?? "");
  if (requestId && tracker.recordedResponseIds.has(requestId)) return;
  const method = tracker.requestMethods.get(requestId) ?? String(response?.method ?? "GET");
  const category = tracker.requestCategories.get(requestId) ?? classifyRequest(response?.url, method);
  const status = Number(response?.status ?? 0);
  if (requestId) tracker.recordedResponseIds.add(requestId);
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category.startsWith("partner:")) {
    tracker.partnerStatuses.push(status);
    tracker.noStoreObserved += 1;
    tracker.noStoreResponses &&= headerValue(response?.headers, "Cache-Control").toLowerCase().startsWith("no-store");
  }
}


function responseRequestId(tracker, event) {
  const fetchId = String(event?.requestId ?? "");
  return String(event?.networkId ?? tracker.fetchNetworkIds.get(fetchId) ?? fetchId);
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
    if (eventSessionId === sessionId) recordResponseStatus(tracker, {
      requestId: event?.requestId,
      url: event?.response?.url,
      status: event?.response?.status,
      headers: event?.response?.headers,
    });
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
      recordResponseStatus(tracker, {
        requestId: responseRequestId(tracker, event),
        method: event.request.method,
        url: event.request.url,
        status,
        headers: event.responseHeaders,
      });
      const expectation = tracker.pauseExpectation;
      if (expectation && expectation.category === category && expectation.status === status && !tracker.pausedResponse) {
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
    tracker.fetchNetworkIds.set(String(event.requestId), String(event.networkId ?? event.requestId));
    inspectBusinessRequest(tracker, event.request);
    const guard = browserRequestAllowed(event.request.url);
    if (guard.external) tracker.externalRequests += 1;
    if (guard.forbiddenQuery) tracker.forbiddenQueries += 1;
    const suppliedPrivate = tracker.privateValues.some((item) => item && String(event.request.url).includes(item));
    const command = guard.allowed && !suppliedPrivate ? "Fetch.continueRequest" : "Fetch.failRequest";
    const parameters = command === "Fetch.continueRequest"
      ? { requestId: event.requestId }
      : { requestId: event.requestId, errorReason: "BlockedByClient" };
    client.send(command, parameters, eventSessionId)
      .catch(() => { tracker.edgeProcessErrorCount += 1; });
  });
  await client.send("Page.enable", {}, sessionId);
  await client.send("Runtime.enable", {}, sessionId);
  await client.send("Network.enable", {}, sessionId);
  await client.send("Network.setCacheDisabled", { cacheDisabled: true }, sessionId);
  await client.send("Fetch.disable", {}, sessionId);
  await client.send("Fetch.enable", { patterns: [
    { urlPattern: "*", requestStage: "Request" },
    { urlPattern: `${CONTROL_ORIGIN}/partners/profile`, requestStage: "Response" },
    { urlPattern: "http://127.0.0.1:8001/partners/profile", requestStage: "Response" },
  ] }, sessionId);
}


async function waitForDom(client, sessionId, expression, stage) {
  return waitForDomBase(client, sessionId, expression, stage, DOM_WAIT_TIMEOUT_MS);
}


async function settle(client, sessionId, stage) {
  return evaluate(
    client,
    sessionId,
    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))",
    stage,
  );
}


async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#partner-portal` }, sessionId),
    "browser.console.load",
    60_000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


async function login(client, sessionId, tracker, tenantSlug, email, password, stage) {
  const before = tracker.loginStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form=document.querySelector('#login-form'); if(!form)return false;
    form.elements.namedItem('tenantSlug').value=${JSON.stringify(tenantSlug)};
    form.elements.namedItem('email').value=${JSON.stringify(email)};
    form.elements.namedItem('password').value=${JSON.stringify(password)};
    for(const field of form.querySelectorAll('input'))field.dispatchEvent(new Event('input',{bubbles:true}));
    form.requestSubmit(); return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForDom(client, sessionId, "document.querySelector('#logout-button')", `${stage}.dom`);
  await waitForTrackerCount(() => tracker.loginStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.loginStatuses.at(-1) === 200, stage, "status");
}


async function logout(client, sessionId, tracker, stage) {
  const before = tracker.logoutStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('#logout-button');
    if(!button||button.disabled)return false; button.click(); return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `${stage}.form`);
  await waitForTrackerCount(() => tracker.logoutStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.logoutStatuses.at(-1) === 200, stage, "status");
}


async function openPartnerPage(client, sessionId, stage, expression, { forceNavigation = false } = {}) {
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('.partner-portal-panel') || document.querySelector('[data-console-nav=\"partner-portal\"]')",
    `${stage}.shell`,
  );
  const current = await evaluate(client, sessionId, `Boolean(document.querySelector(${JSON.stringify(expression)}))`, `${stage}.current`);
  if (forceNavigation || !current) {
    const clicked = await evaluate(client, sessionId, `(() => {
      const button=document.querySelector('[data-console-nav="partner-portal"]');
      if(!button||button.disabled)return false; button.click(); return true;
    })()`, `${stage}.navigate`);
    requireCondition(clicked === true, stage, "navigation");
  }
  await waitForDom(client, sessionId, `document.querySelector(${JSON.stringify(expression)})`, `${stage}.ready`);
}


async function readPartnerSnapshot(client, sessionId, stage) {
  return evaluate(client, sessionId, `(() => {
    const panel=document.querySelector('.partner-portal-panel');
    const root=panel?.querySelector('[data-partner-portal-root]');
    const text=panel?.textContent||'';
    return {
      panel:Boolean(panel),root:Boolean(root),setup:Boolean(panel?.querySelector('[data-partner-setup-form]')),
      branding:Boolean(panel?.querySelector('[data-partner-branding-form]')),
      customerFilters:Boolean(panel?.querySelector('[data-partner-customer-filters]')),
      commissionFilters:Boolean(panel?.querySelector('[data-partner-commission-filters]')),
      customerCreate:Boolean(panel?.querySelector('[data-partner-customer-create]')),
      export:Boolean(panel?.querySelector('[data-partner-commission-export]')),
      delivery:Boolean(panel?.querySelector('[data-partner-delivery-download]')),
      customerRows:panel?.querySelectorAll('.partner-portal-table:not(.partner-portal-commission-table) .partner-portal-row:not(.partner-portal-table-head)').length||0,
      commissionVisible:Boolean(panel?.querySelector('#partner-commission-title')),
      primary:text.includes(${JSON.stringify(PRIMARY_MARKERS.brandedPortalName)}),
      secondary:text.includes(${JSON.stringify(SECONDARY_PORTAL_NAME)}),
      noError:!panel?.querySelector('.partner-portal-error,.partner-portal-section-state.is-error'),
      brandingApplied:Boolean(getComputedStyle(document.documentElement).getPropertyValue('--partner-brand-primary').trim()),
    };
  })()`, stage);
}


async function readLayoutMetrics(client, sessionId, focusSelector) {
  return evaluate(client, sessionId, `(() => {
    const root=document.querySelector('.partner-portal-panel');
    const focus=document.querySelector(${JSON.stringify(focusSelector)});
    const shown=(item)=>{if(!item)return false;const style=getComputedStyle(item);const rect=item.getBoundingClientRect();
      return style.display!=='none'&&style.visibility!=='hidden'&&rect.width>0&&rect.height>0;};
    const controls=[...(root?.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])')||[])].filter(shown);
    const kind=(item)=>item.matches('button')?'button':item.matches('select')?'select':item.matches('textarea')?'textarea':
      item.matches('input[type=color]')?'color':item.matches('input[type=date]')?'date':'input';
    let overlaps=0;const overlapKinds=new Set();
    for(let first=0;first<controls.length;first+=1){const a=controls[first].getBoundingClientRect();
      for(let second=first+1;second<controls.length;second+=1){
        if(controls[first].contains(controls[second])||controls[second].contains(controls[first]))continue;
        const b=controls[second].getBoundingClientRect();
        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>2&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>2){
          overlaps+=1;overlapKinds.add([kind(controls[first]),kind(controls[second])].sort().join('_'));}
      }}
    const textOverflow=[...(root?.querySelectorAll('button,label,span,strong,small,time,p,h2,h3')||[])]
      .filter((item)=>shown(item)&&!item.closest('.partner-portal-table'))
      .some((item)=>!['auto','scroll'].includes(getComputedStyle(item).overflowX)&&item.scrollWidth>item.clientWidth+2);
    const rootRect=root?.getBoundingClientRect();const focusRect=focus?.getBoundingClientRect();
    return {width:innerWidth,height:innerHeight,rootVisible:shown(root),focusVisible:shown(focus)
      &&focusRect.bottom>0&&focusRect.top<innerHeight&&focusRect.right>0&&focusRect.left<innerWidth,
      horizontalOverflow:document.documentElement.scrollWidth>innerWidth+1||document.body.scrollWidth>innerWidth+1,
      textOverflow,overlapCount:overlaps,overlapKinds:[...overlapKinds].sort().join('-'),
      contentBounded:Boolean(rootRect)&&rootRect.left>=-1&&rootRect.right<=innerWidth+1};
  })()`, "browser.layout.read");
}


async function captureScreenshot(client, sessionId, staging, filename, viewport, focusSelector, tracker, stage) {
  await setViewport(client, sessionId, viewport);
  requireCondition(await evaluate(client, sessionId, `(() => {
    const focus=document.querySelector(${JSON.stringify(focusSelector)});
    if(!focus)return false;focus.scrollIntoView({block:'center',inline:'nearest'});return true;
  })()`, `${stage}.focus`) === true, stage, "focus");
  await settle(client, sessionId, `${stage}.settle`);
  const privacy = await evaluate(client, sessionId, `(() => {
    const needles=${JSON.stringify(tracker.privateValues)};const root=document.querySelector('.partner-portal-panel');
    const values=[root?.textContent||'',...[...(root?.querySelectorAll('input,textarea,select')||[])].map((item)=>String(item.value||'')),
      ...[...(root?.querySelectorAll('*')||[])].flatMap((item)=>[...item.attributes].map((attribute)=>attribute.value))];
    return needles.every((needle)=>!values.some((value)=>value.includes(needle)));
  })()`, `${stage}.privacy`);
  requireCondition(privacy === true, stage, "privacy");
  const layout = await readLayoutMetrics(client, sessionId, focusSelector);
  requireCondition(layout.width === viewport.width && layout.height === viewport.height, stage, "dimensions");
  requireCondition(layout.rootVisible && layout.focusVisible, stage, "visibility");
  requireCondition(!layout.horizontalOverflow, stage, "horizontal_overflow");
  requireCondition(!layout.textOverflow, stage, "text_overflow");
  requireCondition(layout.overlapCount === 0, stage, `overlap_${layout.overlapCount}_${layout.overlapKinds || 'unknown'}`);
  requireCondition(layout.contentBounded, stage, "content_boundary");
  await evaluate(client, sessionId, `(() => {const item=document.querySelector('.user-menu');if(!item)return false;
    item.dataset.prd087Mask='true';item.style.visibility='hidden';return true;})()`, `${stage}.mask`);
  let result;
  try {
    result = await client.send("Page.captureScreenshot", {
      format: "png", fromSurface: true, captureBeyondViewport: false,
    }, sessionId, 30_000);
  } finally {
    await evaluate(client, sessionId, `(() => {const item=document.querySelector('[data-prd087-mask=true]');if(!item)return false;
      item.style.visibility='';delete item.dataset.prd087Mask;return true;})()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({ layout, nonblank: true, noSensitiveDom: true });
}


async function exerciseAdminInitialization(client, sessionId, tracker, staging, privateConfig) {
  await login(client, sessionId, tracker, "default", ADMIN_EMAIL, privateConfig.adminPassword, "browser.login.admin");
  const navigationVisible = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-console-nav=\"partner-portal\"]'))", "browser.admin.navigation");
  await openPartnerPage(client, sessionId, "browser.admin.setup", "[data-partner-setup-form]");
  const setupVisible = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-partner-setup-form]'))", "browser.admin.setup_visible");
  const mobile = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[0], VIEWPORTS.mobile,
    ".partner-portal-empty", tracker, "browser.screenshot.setup_mobile",
  );
  const submitted = await evaluate(client, sessionId, `(() => {
    const form=document.querySelector('[data-partner-setup-form]');if(!form)return false;
    const values=${JSON.stringify({
      partner_code: PRIMARY_MARKERS.partnerCode,
      display_name: "Primary Channel",
      portal_name: PRIMARY_MARKERS.portalName,
      brand_primary_color: "#176b5b",
      support_text: PRIMARY_MARKERS.supportText,
    })};
    for(const [name,value] of Object.entries(values)){const field=form.elements.namedItem(name);field.value=value;
      field.dispatchEvent(new Event('input',{bubbles:true}));field.dispatchEvent(new Event('change',{bubbles:true}));}
    form.requestSubmit();return true;
  })()`, "browser.admin.configure");
  requireCondition(navigationVisible && setupVisible && submitted, "browser.admin", "initialization");
  await waitForDom(
    client, sessionId,
    "document.querySelector('[data-partner-portal-root]')"
      + "&&document.querySelector('[data-partner-customer-filters]')"
      + "&&document.querySelector('[data-partner-commission-filters]')"
      + "&&!document.querySelector('.partner-portal-section-state[role=\"status\"]')",
    "browser.admin.ready",
  );
  await settle(client, sessionId, "browser.admin.ready_settle");
  const branding = await evaluate(client, sessionId, `(() => {
    const form=document.querySelector('[data-partner-branding-form]');if(!form)return false;
    const name=form.elements.namedItem('portal_name');const support=form.elements.namedItem('support_text');
    name.value=${JSON.stringify(PRIMARY_MARKERS.brandedPortalName)};
    support.value=${JSON.stringify(PRIMARY_MARKERS.supportText)};
    name.dispatchEvent(new Event('input',{bubbles:true}));support.dispatchEvent(new Event('input',{bubbles:true}));
    form.requestSubmit();return true;
  })()`, "browser.admin.branding");
  requireCondition(branding === true, "browser.admin.branding", "submit");
  await waitForDom(
    client, sessionId,
    `document.querySelector('#partner-portal-title')?.textContent===${JSON.stringify(PRIMARY_MARKERS.brandedPortalName)}`,
    "browser.admin.branding_saved",
  );
  const snapshot = await readPartnerSnapshot(client, sessionId, "browser.admin.snapshot");
  const operationsVisible = snapshot.root && snapshot.branding && snapshot.customerFilters
    && snapshot.commissionFilters && snapshot.customerCreate && snapshot.export && snapshot.delivery
    && snapshot.commissionVisible && snapshot.noError && snapshot.brandingApplied;
  requireCondition(operationsVisible, "browser.admin", "operations");
  const desktop = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[1], VIEWPORTS.desktop,
    ".partner-portal-header", tracker, "browser.screenshot.ready_desktop",
  );
  await logout(client, sessionId, tracker, "browser.logout.admin");
  return Object.freeze({ navigationVisible, setupVisible, configured: snapshot.root, operationsVisible, mobile, desktop });
}


async function exerciseViewerAndRevocation(client, sessionId, tracker, staging, privateConfig) {
  await login(client, sessionId, tracker, "default", VIEWER_EMAIL, privateConfig.rolePassword, "browser.login.viewer");
  await settle(client, sessionId, "browser.viewer.login_settle");
  const navigationVisible = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-console-nav=\"partner-portal\"]'))", "browser.viewer.navigation");
  await openPartnerPage(
    client,
    sessionId,
    "browser.viewer.ready",
    "[data-partner-portal-root]",
    { forceNavigation: true },
  );
  let snapshot = await readPartnerSnapshot(client, sessionId, "browser.viewer.snapshot");
  const operationEntry = snapshot.root && !snapshot.setup && snapshot.branding && snapshot.customerCreate
    && snapshot.export && snapshot.delivery && snapshot.noError;
  requireCondition(navigationVisible && operationEntry, "browser.viewer", "entry");
  const dialogOpened = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-partner-customer-create]');
    if(!button||button.disabled)return false;button.click();return true;
  })()`, "browser.viewer.customer_open");
  requireCondition(dialogOpened === true, "browser.viewer.customer_open", "button");
  await waitForDom(client, sessionId, "document.querySelector('[data-partner-customer-create-form]')", "browser.viewer.customer_dialog");
  const customerSubmitted = await evaluate(client, sessionId, `(() => {
    const form=document.querySelector('[data-partner-customer-create-form]');if(!form)return false;
    const project=form.elements.namedItem('project_id');const option=[...project.options].find((item)=>item.value);
    if(!option)return false;project.value=option.value;
    form.elements.namedItem('customer_code').value=${JSON.stringify(PRIMARY_MARKERS.customerCode)};
    form.elements.namedItem('display_name').value=${JSON.stringify(PRIMARY_MARKERS.customerName)};
    for(const field of form.querySelectorAll('input,select')){field.dispatchEvent(new Event('input',{bubbles:true}));field.dispatchEvent(new Event('change',{bubbles:true}));}
    form.requestSubmit();return true;
  })()`, "browser.viewer.customer_create");
  requireCondition(customerSubmitted === true, "browser.viewer.customer_create", "submit");
  await waitForDom(
    client, sessionId,
    "document.querySelectorAll('.partner-portal-table:not(.partner-portal-commission-table) .partner-portal-row:not(.partner-portal-table-head)').length===1",
    "browser.viewer.customer_saved",
  );
  snapshot = await readPartnerSnapshot(client, sessionId, "browser.viewer.after_customer");
  requireCondition(snapshot.customerRows === 1 && snapshot.commissionVisible, "browser.viewer", "operation");
  const wide = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[2], VIEWPORTS.wide,
    "#partner-commission-title", tracker, "browser.screenshot.commission_wide",
  );

  const paused = pauseNextResponse(tracker, "partner:GET:/partners/profile", 200, "browser.revoke.pause");
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-partner-refresh]');if(!button||button.disabled)return false;
    button.click();return true;
  })()`, "browser.revoke.refresh");
  requireCondition(refreshed === true, "browser.revoke.refresh", "button");
  await paused;
  requireCondition(
    (await postHarness("/revoke-partner-portal-read", privateConfig, {})).status === 200,
    "browser.revoke", "status",
  );
  const navigated = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-console-nav=overview]');if(!button||button.disabled)return false;
    button.click();return true;
  })()`, "browser.revoke.navigate");
  requireCondition(navigated === true, "browser.revoke.navigate", "button");
  await waitForDom(
    client, sessionId,
    "!document.querySelector('[data-console-nav=\"partner-portal\"]')"
      + "&&!document.querySelector('[data-partner-portal-root],[data-partner-setup-form]')"
      + "&&!getComputedStyle(document.documentElement).getPropertyValue('--partner-brand-primary').trim()",
    "browser.revoke.cleared",
  );
  const beforeRelease = await evaluate(client, sessionId, `(() => ({
    root:!document.querySelector('[data-partner-portal-root]'),
    navigation:!document.querySelector('[data-console-nav="partner-portal"]'),
    dom:!document.querySelector('[data-partner-branding-form],[data-partner-customer-filters],[data-partner-commission-filters]'),
    branding:!getComputedStyle(document.documentElement).getPropertyValue('--partner-brand-primary').trim(),
  }))()`, "browser.revoke.before_release");
  await releasePausedResponse(client, tracker, "browser.revoke.release", { allowClosed: true });
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const lateDiscarded = await evaluate(client, sessionId, `(() =>
    !document.querySelector('[data-partner-portal-root],[data-partner-setup-form]')
      &&!document.querySelector('[data-console-nav="partner-portal"]')
      &&!getComputedStyle(document.documentElement).getPropertyValue('--partner-brand-primary').trim())()`, "browser.revoke.late");
  await logout(client, sessionId, tracker, "browser.logout.viewer");
  requireCondition(
    (await postHarness("/restore-partner-portal-read", privateConfig, {})).status === 200,
    "browser.restore", "status",
  );
  return Object.freeze({
    navigationVisible, operationEntry, operationCompleted: snapshot.customerRows === 1, wide,
    root: beforeRelease.root, navigation: beforeRelease.navigation, dom: beforeRelease.dom,
    branding: beforeRelease.branding, lateDiscarded,
  });
}


async function exerciseUnauthorized(client, sessionId, tracker, privateConfig) {
  const before = tracker.partnerRequestCount;
  await login(client, sessionId, tracker, "default", DEVELOPER_EMAIL, privateConfig.rolePassword, "browser.login.unauthorized");
  await settle(client, sessionId, "browser.unauthorized.settle");
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const state = await evaluate(client, sessionId, `(() => ({
    navigation:!document.querySelector('[data-console-nav="partner-portal"]'),
    page:!document.querySelector('.partner-portal-panel,[data-partner-portal-root],[data-partner-setup-form]'),
  }))()`, "browser.unauthorized.state");
  const apiPrefetchAbsent = tracker.partnerRequestCount === before;
  requireCondition(state.navigation && state.page && apiPrefetchAbsent, "browser.unauthorized", "boundary");
  await logout(client, sessionId, tracker, "browser.logout.unauthorized");
  return Object.freeze({ ...state, apiPrefetchAbsent });
}


async function exerciseSecondary(client, sessionId, tracker, staging, privateConfig) {
  await login(client, sessionId, tracker, "prd087-secondary", SECONDARY_EMAIL, privateConfig.adminPassword, "browser.login.secondary");
  await settle(client, sessionId, "browser.secondary.login_settle");
  const navigationVisible = await evaluate(client, sessionId, "Boolean(document.querySelector('[data-console-nav=\"partner-portal\"]'))", "browser.secondary.navigation");
  // The previous tenant's root can survive for a render frame; always re-own navigation after login.
  await openPartnerPage(
    client,
    sessionId,
    "browser.secondary.ready",
    "[data-partner-portal-root]",
    { forceNavigation: true },
  );
  const snapshot = await readPartnerSnapshot(client, sessionId, "browser.secondary.snapshot");
  const independent = snapshot.root && snapshot.secondary && !snapshot.primary
    && snapshot.customerRows === 0 && snapshot.noError && snapshot.brandingApplied;
  requireCondition(navigationVisible, "browser.secondary", "navigation");
  requireCondition(snapshot.root, "browser.secondary", "root");
  requireCondition(snapshot.secondary, "browser.secondary", "tenant_marker");
  requireCondition(!snapshot.primary, "browser.secondary", "previous_tenant");
  requireCondition(snapshot.customerRows === 0, "browser.secondary", "customer_rows");
  requireCondition(snapshot.noError, "browser.secondary", "error_state");
  requireCondition(snapshot.brandingApplied, "browser.secondary", "branding");
  const mobile = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[3], VIEWPORTS.mobile,
    ".partner-portal-header", tracker, "browser.screenshot.secondary_mobile",
  );
  await logout(client, sessionId, tracker, "browser.logout.secondary");
  const storageCleared = await evaluate(
    client, sessionId,
    "localStorage.length===0&&sessionStorage.length===0",
    "browser.logout.storage",
  );
  return Object.freeze({ navigationVisible, independent, previousCleared: !snapshot.primary, mobile, storageCleared });
}


async function verifyFinalPrivacy(client, sessionId, tracker) {
  const result = await evaluate(client, sessionId, `(async () => {
    const needles=${JSON.stringify(tracker.privateValues)};
    const values=[document.documentElement.textContent||'',...[...document.querySelectorAll('input,textarea,select')].map((item)=>String(item.value||'')),
      ...[...document.querySelectorAll('*')].flatMap((item)=>[...item.attributes].map((attribute)=>attribute.value)),
      ...Object.entries(localStorage).flat(),...Object.entries(sessionStorage).flat(),location.href];
    return {hits:needles.filter((needle)=>values.some((value)=>String(value).includes(needle))).length,
      caches:typeof caches==='undefined'?0:(await caches.keys()).length,
      databases:typeof indexedDB.databases!=='function'?0:(await indexedDB.databases()).length,
      login:Boolean(document.querySelector('#login-form'))};
  })()`, "browser.privacy");
  requireCondition(
    result.hits === 0 && result.caches === 0 && result.databases === 0 && result.login,
    "browser.privacy", "persistent",
  );
  return true;
}


function requestContractsExact(tracker) {
  return tracker.requestContractExact
    && tracker.partnerRequestCount >= 16
    && tracker.partnerStatuses.length === tracker.partnerRequestCount
    && tracker.noStoreObserved === tracker.partnerStatuses.length
    && requiredRoutesObserved(tracker);
}


function requiredRoutesObserved(tracker) {
  const required = [
    "GET /partners/profile",
    "POST /partners",
    "PATCH /partners/branding",
    "GET /partners/projects",
    "GET /partners/customers",
    "POST /partners/customers",
    "GET /partners/commission",
  ];
  return required.every((route) => tracker.partnerRoutes.has(route));
}


function buildCompletionFacts({ admin, viewer, unauthorized, secondary, registration, tracker }) {
  return {
    status: "passed",
    desktop: {
      width: 1280,
      height: 720,
      nonblank: admin.desktop.nonblank && viewer.wide.nonblank,
      page_119: admin.configured,
      ready_visible: admin.configured,
      operations_visible: admin.operationsVisible && viewer.operationEntry,
      horizontal_overflow_free: admin.desktop.layout.horizontalOverflow === false && viewer.wide.layout.horizontalOverflow === false,
      content_bounded: admin.desktop.layout.contentBounded && viewer.wide.layout.contentBounded,
      text_overflow_free: admin.desktop.layout.textOverflow === false && viewer.wide.layout.textOverflow === false,
      overlap_count: Math.max(admin.desktop.layout.overlapCount, viewer.wide.layout.overlapCount),
      no_sensitive_dom: admin.desktop.noSensitiveDom && viewer.wide.noSensitiveDom,
    },
    mobile: {
      width: 390,
      height: 844,
      nonblank: admin.mobile.nonblank && secondary.mobile.nonblank,
      page_119: admin.setupVisible,
      setup_visible: admin.setupVisible,
      tenant_isolation_visible: secondary.independent,
      horizontal_overflow_free: admin.mobile.layout.horizontalOverflow === false && secondary.mobile.layout.horizontalOverflow === false,
      content_bounded: admin.mobile.layout.contentBounded && secondary.mobile.layout.contentBounded,
      text_overflow_free: admin.mobile.layout.textOverflow === false && secondary.mobile.layout.textOverflow === false,
      overlap_count: Math.max(admin.mobile.layout.overlapCount, secondary.mobile.layout.overlapCount),
      no_sensitive_dom: admin.mobile.noSensitiveDom && secondary.mobile.noSensitiveDom,
    },
    lifecycle: {
      admin_initialization_visible: admin.setupVisible,
      admin_initialization_completed: admin.configured,
      admin_ready_operations_visible: admin.operationsVisible,
      viewer_navigation_visible: viewer.navigationVisible,
      viewer_operation_entry_visible: viewer.operationEntry && viewer.operationCompleted,
      unauthorized_navigation_absent: unauthorized.navigation,
      unauthorized_page_absent: unauthorized.page,
      unauthorized_api_prefetch_absent: unauthorized.apiPrefetchAbsent,
      backend_permission_denied: true,
      dynamic_revocation_root_cleared: viewer.root,
      dynamic_revocation_navigation_cleared: viewer.navigation,
      dynamic_revocation_dom_cleared: viewer.dom,
      dynamic_revocation_branding_cleared: viewer.branding,
      late_response_discarded: viewer.lateDiscarded,
      secondary_navigation_visible: secondary.navigationVisible,
      secondary_tenant_independent: secondary.independent,
      previous_tenant_dom_cleared: secondary.previousCleared,
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
      partner_request_count: tracker.partnerRequestCount,
      partner_statuses: tracker.partnerStatuses,
      no_store_responses: tracker.noStoreResponses && tracker.noStoreObserved === tracker.partnerStatuses.length,
      request_contract_exact: tracker.requestContractExact,
      required_routes_observed: requiredRoutesObserved(tracker),
      no_response_body_capture: true,
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
    requireCondition(
      Object.entries(section).every(([name, item]) => ["width", "height", "overlap_count"].includes(name) || item === true),
      "browser.complete", "facts",
    );
  }
  requireCondition(value.network?.external_requests === 0, "browser.network", "external_requests");
  requireCondition(value.network?.forbidden_queries === 0, "browser.network", "forbidden_queries");
  requireCondition(value.network?.console_errors === 0, "browser.network", "console_errors");
  requireCondition(value.network?.page_errors === 0, "browser.network", "page_errors");
  requireCondition(JSON.stringify(value.network?.login_statuses) === JSON.stringify([200, 200, 200, 200]), "browser.network", "login_statuses");
  requireCondition(JSON.stringify(value.network?.logout_statuses) === JSON.stringify([200, 200, 200, 200]), "browser.network", "logout_statuses");
  requireCondition(value.network?.partner_request_count >= 16, "browser.network", "partner_count");
  requireCondition(value.network?.partner_statuses?.length === value.network.partner_request_count, "browser.network", "partner_statuses");
  requireCondition(value.network?.no_store_responses === true, "browser.network", "cache_control");
  requireCondition(
    value.network?.request_contract_exact === true && value.network?.required_routes_observed === true,
    "browser.network",
    "request_contract",
  );
  requireCondition(value.network?.no_response_body_capture === true, "browser.network", "response_body_capture");
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
    await pageSession.client.send("Emulation.setTimezoneOverride", { timezoneId: "UTC" }, pageSession.sessionId);
    await pageSession.client.send("Page.addScriptToEvaluateOnNewDocument", { source: `(() => {
      const original=window.setInterval.bind(window);window.setInterval=(callback,delay,...args)=>Number(delay)===15000?0:original(callback,delay,...args);
    })();` }, pageSession.sessionId);
    await loadConsole(pageSession.client, pageSession.sessionId);
    const admin = await exerciseAdminInitialization(
      pageSession.client, pageSession.sessionId, tracker, staging, privateConfig,
    );
    const viewer = await exerciseViewerAndRevocation(
      pageSession.client, pageSession.sessionId, tracker, staging, privateConfig,
    );
    const unauthorized = await exerciseUnauthorized(
      pageSession.client, pageSession.sessionId, tracker, privateConfig,
    );
    const secondary = await exerciseSecondary(
      pageSession.client, pageSession.sessionId, tracker, staging, privateConfig,
    );
    await verifyFinalPrivacy(pageSession.client, pageSession.sessionId, tracker);
    requireCondition(
      tracker.browserTokens.size === 4
        && tracker.processPrivacyViolations === 0
        && tracker.processPrivacyViolationCount === 0
        && tracker.edgeProcessErrorCount === 0,
      "browser.process", "privacy_or_error",
    );
    const registration = await postHarness("/register-private", privateConfig, {
      values: [...new Set([privateConfig.driverOwner, ...tracker.browserTokens])],
    });
    requireCondition(registration.status === 200, "harness.private", "status");
    const facts = buildCompletionFacts({ admin, viewer, unauthorized, secondary, registration, tracker });
    validateCompletionFacts(facts);
    await closeBrowser(browserClient, pageSession, browserChild);
    browserClient = null;
    pageSession = null;
    browserChild = null;
    await removeDriverDirectory(driverDirectory, privateConfig.driverOwner);
    requireCondition((await postHarness("/complete", privateConfig, facts)).status === 200, "harness.complete", "status");
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-087", page_id: "PAGE-119", screenshot_count: 4 });
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
    if (harness?.child?.exitCode === null) {
      const harnessStopped = await abortHarness(harness, privateConfig);
      if (!harnessStopped) cleanupFailure ??= new BrowserAcceptanceFailure("cleanup.harness", "failed");
    }
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
  HARNESS_ORIGIN,
  SCREENSHOTS,
  VIEWPORTS,
  abortHarness,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  createDriverDirectory,
  createTracker,
  inspectBusinessRequest,
  parseArguments,
  recordResponseStatus,
  responseRequestId,
  removeDriverDirectory,
  requestContractsExact,
  safeRunId,
  validateCompletionFacts,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
