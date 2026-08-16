/**
 * PRD-082 PAGE-114 real Chromium acceptance driver.
 *
 * The driver uses only repository-owned runtimes, loopback services, and
 * repository-local evidence. Endpoint, bearer, access tokens, idempotency
 * keys, arguments, and result bodies remain private in memory.
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
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd082-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd082-browser-driver");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd082-browser");
const DRIVER_OWNER_FILENAME = ".prd082-browser-driver-owner";
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18885";
const HARNESS_CONSOLE_URL = "http://127.0.0.1:5173/#mcp-tools";
const DEBUG_PORT = 19085;
const DOM_WAIT_TIMEOUT_MS = 90_000;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization|prompt|response|key)/i;
const SERVER_ID = /^mcps_[0-9a-f]{32}$/;
const TOOL_ID = /^mcpt_[0-9a-f]{32}$/;
const ADMIN_EMAIL = "admin@metahub.local";
const OPERATOR_EMAIL = "operator-prd082@local.invalid";
const POLICY_OPERATOR_EMAIL = "policy-operator-prd082@local.invalid";
const VIEWER_EMAIL = "viewer-prd082@local.invalid";
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "page-114-servers-desktop-1280x720.png",
  "page-114-tools-desktop-1280x720.png",
  "page-114-invocations-mobile-390x844.png",
  "page-114-register-mobile-390x844.png",
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
  const endpointHost = randomBytes(18).toString("hex");
  return Object.freeze({
    adminPassword: `Prd082-Admin-${randomBytes(40).toString("base64url")}-Aa1`,
    rolePassword: `Prd082-Role-${randomBytes(40).toString("base64url")}-Aa1`,
    stopToken: `Prd082-Stop-${randomBytes(48).toString("base64url")}`,
    driverOwner: randomBytes(32).toString("hex"),
    endpoint: `https://mcp-${endpointHost}.invalid/private`,
    bearer: `Prd082-Bearer-${randomBytes(48).toString("base64url")}`,
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
    PRD082_BROWSER_ADMIN_PASSWORD: privateConfig.adminPassword,
    PRD082_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD082_BROWSER_STOP_TOKEN: privateConfig.stopToken,
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
  {
    mkdirOperation = mkdir,
    writeOperation = writeFile,
    readOperation = readFile,
    removeOperation = rm,
  } = {},
) {
  requireCondition(/^[0-9a-f]{64}$/.test(String(owner)), "config.driver_directory", "owner");
  await mkdirOperation(DRIVER_ROOT, { recursive: true });
  const directory = directChild(DRIVER_ROOT, runId, "config.driver_directory");
  requireCondition(!existsSync(directory), "config.driver_directory", "exists");
  let created = false;
  try {
    await mkdirOperation(directory);
    created = true;
    const marker = path.join(directory, DRIVER_OWNER_FILENAME);
    await writeOperation(marker, `${owner}\n`, "ascii");
    const claim = await readOperation(marker, "ascii");
    requireCondition(claim.trim() === owner, "config.driver_directory", "owner");
    return directory;
  } catch (error) {
    if (created) {
      try {
        await removeOperation(directory, { recursive: true, force: false });
      } catch {
        throw new BrowserAcceptanceFailure("config.driver_directory", "rollback");
      }
    }
    throw error;
  }
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
      const match = stderrBuffer.match(
        /PRD-082 browser harness failed: ([a-z][a-z0-9_.-]{1,100}):([a-z][a-z0-9_.-]{1,100})/,
      );
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
  const drained = () => streams.every(
    (stream) => stream.readableEnded === true || stream.closed === true,
  );
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
    privateConfig.endpoint,
    privateConfig.bearer,
  ];
  return {
    privateValues,
    secrets: privateValues,
    browserTokens: new Set(),
    idempotencyKeys: new Set(),
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
    registrationStatuses: [],
    refreshStatuses: [],
    serverStatuses: [],
    policyStatuses: [],
    listSuccesses: 0,
    detailSuccesses: 0,
    noStoreResponses: true,
    noStoreObserved: 0,
    requestMethods: new Map(),
    requestCategories: new Map(),
    requestContractsExact: true,
    requestContractCounts: {
      registration: 0,
      refresh: 0,
      serverStatus: 0,
      policy: 0,
    },
    serverStatusValues: [],
    pauseExpectation: null,
    pausedResponse: null,
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
  const forbiddenQuery = [...target.searchParams.keys()].some(
    (name) => FORBIDDEN_QUERY_KEY.test(name),
  );
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
  if (["http://localhost:5173", "http://127.0.0.1:5173"].includes(target.origin)) {
    return "console_asset";
  }
  if (target.origin === HARNESS_ORIGIN) return "harness";
  if (target.pathname === "/auth/login" && verb === "POST") return "auth_login";
  if (target.pathname === "/auth/logout" && verb === "POST") return "auth_logout";
  if (target.pathname === "/mcp/servers" && verb === "GET") return "server_list";
  if (target.pathname === "/mcp/servers" && verb === "POST") return "server_registration";
  if (/^\/mcp\/servers\/mcps_[0-9a-f]{32}\/refresh-tools$/.test(target.pathname)
    && verb === "POST") return "server_refresh";
  if (/^\/mcp\/servers\/mcps_[0-9a-f]{32}$/.test(target.pathname) && verb === "PATCH") {
    return "server_status";
  }
  if (target.pathname === "/mcp/tools" && verb === "GET") return "tool_list";
  if (/^\/mcp\/tools\/mcpt_[0-9a-f]{32}$/.test(target.pathname) && verb === "GET") {
    return "tool_detail";
  }
  if (/^\/mcp\/tools\/mcpt_[0-9a-f]{32}$/.test(target.pathname) && verb === "PATCH") {
    return "tool_policy";
  }
  if (target.pathname === "/mcp/invocations" && verb === "GET") return "invocation_list";
  if (target.pathname.startsWith("/console/")) return "console_navigation";
  return "control_local";
}


function headerValue(headers, name) {
  const entry = Object.entries(headers ?? {}).find(
    ([key]) => key.toLowerCase() === String(name).toLowerCase(),
  );
  return entry ? String(entry[1]) : "";
}


function exactKeys(value, names) {
  return value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...names].sort());
}


function parsePostData(request) {
  try { return JSON.parse(String(request?.postData ?? "")); } catch { return null; }
}


function inspectBusinessRequest(tracker, request, privateConfig) {
  const category = classifyRequest(request?.url, request?.method);
  const authorization = headerValue(request?.headers, "Authorization");
  const browserToken = authorization.match(/^Bearer ([^\s]+)$/)?.[1] ?? "";
  if (browserToken) {
    tracker.browserTokens.add(browserToken);
    if (!tracker.privateValues.includes(browserToken)) tracker.privateValues.push(browserToken);
  }
  const idempotencyKey = headerValue(request?.headers, "Idempotency-Key");
  if (idempotencyKey) {
    tracker.idempotencyKeys.add(idempotencyKey);
    if (!tracker.privateValues.includes(idempotencyKey)) tracker.privateValues.push(idempotencyKey);
  }
  if (!["server_registration", "server_refresh", "server_status", "tool_policy"].includes(category)) {
    return;
  }
  const payload = parsePostData(request);
  const safeIdentity = !Object.hasOwn(payload ?? {}, "tenant_id")
    && !Object.hasOwn(payload ?? {}, "actor_user_id")
    && !Object.hasOwn(payload ?? {}, "user_id");
  if (category === "server_registration") {
    const exact = exactKeys(payload, ["name", "endpoint_url", "auth"])
      && payload.name === "Browser Registered Server"
      && payload.endpoint_url === privateConfig.endpoint
      && exactKeys(payload.auth, ["type", "bearer_token"])
      && payload.auth.type === "bearer"
      && payload.auth.bearer_token === privateConfig.bearer
      && idempotencyKey.length >= 8
      && safeIdentity;
    tracker.requestContractsExact &&= exact;
    tracker.requestContractCounts.registration += 1;
  }
  if (category === "server_refresh") {
    const exact = exactKeys(payload, ["expected_revision"])
      && Number.isSafeInteger(payload.expected_revision)
      && payload.expected_revision >= 1
      && idempotencyKey.length >= 8
      && safeIdentity;
    tracker.requestContractsExact &&= exact;
    tracker.requestContractCounts.refresh += 1;
  }
  if (category === "server_status") {
    const exact = exactKeys(payload, ["expected_revision", "status"])
      && Number.isSafeInteger(payload.expected_revision)
      && payload.expected_revision >= 1
      && ["disabled", "active"].includes(payload.status)
      && idempotencyKey.length >= 8
      && safeIdentity;
    tracker.requestContractsExact &&= exact;
    tracker.requestContractCounts.serverStatus += 1;
    if (["disabled", "active"].includes(payload?.status)) {
      tracker.serverStatusValues.push(payload.status);
    }
  }
  if (category === "tool_policy") {
    const exact = exactKeys(payload, [
      "expected_policy_revision",
      "status",
      "risk_level",
      "approval_required",
      "approval_expires_seconds",
      "required_permission_codes",
      "allowed_role_ids",
      "allowed_user_ids",
      "result_redaction_pointers",
    ])
      && Number.isSafeInteger(payload.expected_policy_revision)
      && payload.expected_policy_revision >= 1
      && payload.status === "active"
      && payload.risk_level === "high"
      && payload.approval_required === true
      && Number.isSafeInteger(payload.approval_expires_seconds)
      && payload.approval_expires_seconds >= 300
      && payload.approval_expires_seconds <= 86400
      && Array.isArray(payload.required_permission_codes)
      && Array.isArray(payload.allowed_role_ids)
      && payload.allowed_role_ids.length === 1
      && Array.isArray(payload.allowed_user_ids)
      && payload.allowed_user_ids.length === 0
      && Array.isArray(payload.result_redaction_pointers)
      && idempotencyKey === ""
      && safeIdentity;
    tracker.requestContractsExact &&= exact;
    tracker.requestContractCounts.policy += 1;
  }
}


function isMcpCategory(category) {
  return [
    "server_list",
    "server_registration",
    "server_refresh",
    "server_status",
    "tool_list",
    "tool_detail",
    "tool_policy",
    "invocation_list",
  ].includes(category);
}


function recordResponseStatus(tracker, event) {
  const requestId = String(event?.requestId ?? "");
  const method = tracker.requestMethods.get(requestId) ?? "GET";
  const category = tracker.requestCategories.get(requestId)
    ?? classifyRequest(event?.response?.url, method);
  const status = Number(event?.response?.status ?? 0);
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category === "server_registration") tracker.registrationStatuses.push(status);
  if (category === "server_refresh") tracker.refreshStatuses.push(status);
  if (category === "server_status") tracker.serverStatuses.push(status);
  if (category === "tool_policy") tracker.policyStatuses.push(status);
  if (["server_list", "tool_list", "invocation_list"].includes(category) && status === 200) {
    tracker.listSuccesses += 1;
  }
  if (category === "tool_detail" && status === 200) tracker.detailSuccesses += 1;
  if (isMcpCategory(category)) {
    tracker.noStoreObserved += 1;
    const cacheControl = headerValue(event?.response?.headers, "Cache-Control");
    tracker.noStoreResponses &&= cacheControl.toLowerCase().startsWith("no-store");
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


async function bindBrowserGuards(client, sessionId, tracker, privateConfig) {
  client.on("Network.requestWillBeSent", (event, eventSessionId) => {
    if (eventSessionId !== sessionId || !event?.request?.url) return;
    if (typeof event.requestId === "string") {
      const method = String(event.request.method ?? "GET");
      tracker.requestMethods.set(event.requestId, method);
      tracker.requestCategories.set(event.requestId, classifyRequest(event.request.url, method));
    }
  });
  client.on("Network.responseReceived", (event, eventSessionId) => {
    if (eventSessionId === sessionId) recordResponseStatus(tracker, event);
  });
  client.on("Network.loadingFailed", (event, eventSessionId) => {
    if (eventSessionId !== sessionId) return;
    const category = tracker.requestCategories.get(String(event.requestId ?? ""));
    if (event.canceled === true && ["server_list", "tool_list", "invocation_list"].includes(category)) {
      tracker.abortEvents += 1;
    }
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
    const responseStage = Number.isFinite(Number(event.responseStatusCode));
    if (responseStage) {
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
    inspectBusinessRequest(tracker, event.request, privateConfig);
    const guard = browserRequestAllowed(event.request.url);
    if (guard.external) tracker.externalRequests += 1;
    if (guard.forbiddenQuery) tracker.forbiddenQueries += 1;
    const suppliedPrivate = tracker.privateValues.some(
      (item) => item && String(event.request.url).includes(item),
    );
    const command = guard.allowed && !suppliedPrivate
      ? "Fetch.continueRequest"
      : "Fetch.failRequest";
    const parameters = command === "Fetch.continueRequest"
      ? { requestId: event.requestId }
      : { requestId: event.requestId, errorReason: "BlockedByClient" };
    client.send(command, parameters, eventSessionId).catch(() => {
      tracker.edgeProcessErrorCount += 1;
    });
  });
  await client.send("Page.enable", {}, sessionId);
  await client.send("Runtime.enable", {}, sessionId);
  await client.send("Network.enable", {}, sessionId);
  await client.send("Network.setCacheDisabled", { cacheDisabled: true }, sessionId);
  await client.send("Fetch.disable", {}, sessionId);
  await client.send("Fetch.enable", { patterns: [
    { urlPattern: "*", requestStage: "Request" },
    { urlPattern: `${CONTROL_ORIGIN}/mcp/servers*`, requestStage: "Response" },
    { urlPattern: `${CONTROL_ORIGIN}/mcp/tools*`, requestStage: "Response" },
    { urlPattern: `${CONTROL_ORIGIN}/mcp/invocations*`, requestStage: "Response" },
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


async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#mcp-tools` }, sessionId),
    "browser.console.load",
    60_000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


async function login(client, sessionId, tracker, account, expectedExpression, stage) {
  const before = tracker.loginStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#login-form');
    if (!form) return false;
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
  await waitForDom(client, sessionId, expectedExpression, `${stage}.dom`);
  await waitForTrackerCount(
    () => tracker.loginStatuses.length,
    before + 1,
    `${stage}.network`,
    60_000,
  );
  requireCondition(tracker.loginStatuses.at(-1) === 200, stage, "status");
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
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `${stage}.form`);
  await waitForTrackerCount(
    () => tracker.logoutStatuses.length,
    before + 1,
    `${stage}.network`,
    60_000,
  );
  requireCondition(tracker.logoutStatuses.at(-1) === 200, stage, "status");
}


async function navigateTo(client, sessionId, itemId, rootSelector, stage) {
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-console-nav=${JSON.stringify(itemId)}]');
    if (!button || button.disabled) return false;
    button.scrollIntoView({ block: 'center', inline: 'nearest' });
    button.click();
    return location.hash === ${JSON.stringify(`#${itemId}`)};
  })()`, stage);
  requireCondition(clicked === true, stage, "button_or_hash");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector(${JSON.stringify(rootSelector)})`,
    `${stage}.root`,
  );
}


function combineShots(first, second) {
  return Object.freeze({
    layout: {
      width: first.layout.width,
      height: first.layout.height,
      horizontalOverflow: first.layout.horizontalOverflow || second.layout.horizontalOverflow,
      textOverflow: first.layout.textOverflow || second.layout.textOverflow,
      overlapCount: Math.max(first.layout.overlapCount, second.layout.overlapCount),
    },
    nonblank: first.nonblank && second.nonblank,
    noSensitiveDom: first.noSensitiveDom && second.noSensitiveDom,
    nextContentHint: first.nextContentHint && second.nextContentHint,
  });
}


async function readLayoutMetrics(client, sessionId, focusSelector) {
  return evaluate(client, sessionId, `(() => {
    const root = document.querySelector('[data-mcp-root]');
    const dialog = root?.querySelector('dialog[open]');
    const scope = dialog || root;
    const focus = document.querySelector(${JSON.stringify(focusSelector)});
    const shown = (item) => {
      if (!item) return false;
      const style = getComputedStyle(item);
      const rect = item.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden'
        && rect.width > 0 && rect.height > 0;
    };
    const controls = [...(scope?.querySelectorAll(
      'button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])',
    ) || [])].filter(shown);
    let overlaps = 0;
    for (let first = 0; first < controls.length; first += 1) {
      const a = controls[first].getBoundingClientRect();
      for (let second = first + 1; second < controls.length; second += 1) {
        if (controls[first].contains(controls[second]) || controls[second].contains(controls[first])) continue;
        const b = controls[second].getBoundingClientRect();
        const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (width > 2 && height > 2) overlaps += 1;
      }
    }
    const textNodes = [...(scope?.querySelectorAll(
      'button,label,span,strong,small,dt,dd,th,td,p,h2,h3,h4',
    ) || [])].filter((item) => {
      if (!shown(item)) return false;
      const style = getComputedStyle(item);
      return !['auto', 'scroll'].includes(style.overflowX)
        && !String(style.whiteSpace).startsWith('pre');
    });
    const focusRect = focus?.getBoundingClientRect();
    return {
      width: innerWidth,
      height: innerHeight,
      rootVisible: shown(root),
      focusVisible: shown(focus) && Boolean(focusRect)
        && focusRect.bottom > 0 && focusRect.top < innerHeight
        && focusRect.right > 0 && focusRect.left < innerWidth,
      horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1
        || document.body.scrollWidth > innerWidth + 1,
      textOverflow: textNodes.some((item) => item.scrollWidth > item.clientWidth + 2),
      overlapCount: overlaps,
      nextContentHint: Boolean(root?.querySelector('.mcp-tab-panel')),
    };
  })()`, "browser.layout.read");
}


function assertLayout(metrics, viewport, stage) {
  requireCondition(
    metrics?.width === viewport.width && metrics.height === viewport.height,
    stage,
    "viewport",
  );
  requireCondition(metrics.rootVisible && metrics.focusVisible, stage, "visibility");
  requireCondition(metrics.horizontalOverflow === false, stage, "horizontal_overflow");
  requireCondition(metrics.textOverflow === false, stage, "text_overflow");
  requireCondition(metrics.overlapCount === 0, stage, "overlap");
  requireCondition(metrics.nextContentHint === true, stage, "next_content");
  return metrics;
}


async function captureScreenshot(
  client,
  sessionId,
  staging,
  filename,
  viewport,
  focusSelector,
  tracker,
  stage,
) {
  await setViewport(client, sessionId, viewport);
  const focused = await evaluate(client, sessionId, `(() => {
    const focus = document.querySelector(${JSON.stringify(focusSelector)});
    if (!focus) return false;
    const rect = focus.getBoundingClientRect();
    if (rect.bottom <= 0 || rect.top >= innerHeight || rect.right <= 0 || rect.left >= innerWidth) {
      focus.scrollIntoView({ block: 'start', inline: 'nearest' });
    }
    return true;
  })()`, `${stage}.focus`);
  requireCondition(focused === true, `${stage}.focus`, "missing");
  await settle(client, sessionId, `${stage}.settle`);
  const privacy = await evaluate(client, sessionId, `(() => {
    const needles = ${JSON.stringify(tracker.privateValues)};
    const root = document.querySelector('[data-mcp-root]');
    const text = root?.textContent || '';
    const values = [...(root?.querySelectorAll('input,textarea,select') || [])]
      .map((item) => String(item.value || ''));
    const attributes = [...(root?.querySelectorAll('*') || [])]
      .flatMap((item) => [...item.attributes].map((attribute) => attribute.value));
    return {
      privateHits: needles.reduce((count, needle) => count
        + Number(text.includes(needle))
        + values.filter((item) => item.includes(needle)).length
        + attributes.filter((item) => item.includes(needle)).length, 0),
      resultDisclosure: Boolean(root?.querySelector('[data-mcp-invocation-result], [data-mcp-arguments]')),
    };
  })()`, `${stage}.privacy`);
  requireCondition(
    privacy.privateHits === 0 && privacy.resultDisclosure === false,
    `${stage}.privacy`,
    "content",
  );
  const layout = assertLayout(
    await readLayoutMetrics(client, sessionId, focusSelector),
    viewport,
    `${stage}.layout`,
  );
  const masked = await evaluate(client, sessionId, `(() => {
    const menu = document.querySelector('.user-menu');
    if (!menu) return false;
    menu.dataset.prd082Mask = 'true';
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
      const menu = document.querySelector('[data-prd082-mask=true]');
      if (!menu) return false;
      menu.style.visibility = '';
      delete menu.dataset.prd082Mask;
      return true;
    })()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({
    layout,
    noSensitiveDom: true,
    nonblank: true,
    nextContentHint: layout.nextContentHint,
  });
}


async function openRegistrationDialog(client, sessionId, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-mcp-open-register]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(opened === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('dialog[data-mcp-dialog=register][open]')",
    `${stage}.dialog`,
  );
  const native = await evaluate(client, sessionId, `(() => {
    const dialog = document.querySelector('dialog[data-mcp-dialog=register]');
    return dialog instanceof HTMLDialogElement && dialog.open
      && dialog.contains(document.activeElement)
      && Boolean(dialog.querySelector('[data-mcp-register-form]'));
  })()`, `${stage}.native`);
  requireCondition(native === true, stage, "native");
  return native;
}


async function submitRegistration(client, sessionId, tracker, privateConfig, stage) {
  const before = tracker.registrationStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-mcp-register-form]');
    if (!form) return false;
    const name = form.elements.namedItem('name');
    const endpoint = form.elements.namedItem('endpoint_url');
    const auth = form.elements.namedItem('auth_type');
    const bearer = form.elements.namedItem('bearer_token');
    name.value = 'Browser Registered Server';
    endpoint.value = ${JSON.stringify(privateConfig.endpoint)};
    auth.value = 'bearer';
    auth.dispatchEvent(new Event('change', { bubbles: true }));
    bearer.value = ${JSON.stringify(privateConfig.bearer)};
    for (const field of [name, endpoint, bearer]) {
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('dialog[data-mcp-dialog=register]')"
      + " && document.querySelectorAll('.mcp-server-table tbody tr').length === 2",
    `${stage}.complete`,
  );
  await waitForTrackerCount(
    () => tracker.registrationStatuses.length,
    before + 1,
    `${stage}.network`,
    60_000,
  );
  requireCondition(tracker.registrationStatuses.at(-1) === 202, stage, "status");
  const erased = await evaluate(client, sessionId, `(() => {
    const root = document.querySelector('[data-mcp-root]');
    const text = root?.textContent || '';
    const values = [...(root?.querySelectorAll('input,textarea') || [])]
      .map((item) => String(item.value || ''));
    const privateValues = ${JSON.stringify([privateConfig.endpoint, privateConfig.bearer])};
    return privateValues.every((value) => !text.includes(value) && !values.includes(value));
  })()`, `${stage}.erase`);
  requireCondition(erased === true, stage, "private_dom");
}


async function clickServerAction(client, sessionId, tracker, kind, expectedStatus, stage) {
  const category = kind === "refresh" ? "refreshStatuses" : "serverStatuses";
  const selector = kind === "refresh"
    ? "[data-mcp-refresh-server]"
    : "[data-mcp-server-status]";
  const before = tracker[category].length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const row = [...document.querySelectorAll('.mcp-server-table tbody tr')]
      .find((item) => item.querySelector('th strong')?.textContent.trim() === 'Controlled Server');
    const selector = ${JSON.stringify(selector)};
    const button = row?.querySelector(selector);
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForTrackerCount(
    () => tracker[category].length,
    before + 1,
    `${stage}.network`,
    60_000,
  );
  requireCondition(tracker[category].at(-1) === expectedStatus, stage, "status");
  await waitForDom(
    client,
    sessionId,
    `(() => {
      const row = [...document.querySelectorAll('.mcp-server-table tbody tr')]
        .find((item) => item.querySelector('th strong')?.textContent.trim() === 'Controlled Server');
      const button = row?.querySelector(${JSON.stringify(selector)});
      return Boolean(button && !button.disabled);
    })()`,
    `${stage}.settled`,
  );
}


async function refreshCurrentView(client, sessionId, tracker, stage) {
  const before = tracker.listSuccesses;
  const marker = `prd082-${randomBytes(12).toString("hex")}`;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-mcp-refresh]');
    if (!button || button.disabled) return false;
    button.setAttribute('data-prd082-refresh-marker', ${JSON.stringify(marker)});
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForTrackerCount(
    () => tracker.listSuccesses,
    before + 1,
    `${stage}.network`,
    60_000,
  );
  await waitForDom(
    client,
    sessionId,
    `!document.querySelector('[data-prd082-refresh-marker=${JSON.stringify(marker)}]')`
      + " && Boolean(document.querySelector('[data-mcp-refresh]:not([disabled])'))",
    `${stage}.settled`,
  );
}


async function switchTab(client, sessionId, tab, readyExpression, stage) {
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-mcp-tab=${JSON.stringify(tab)}]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(client, sessionId, readyExpression, `${stage}.ready`);
}


async function openToolDialog(client, sessionId, kind, stage) {
  const clicked = await evaluate(client, sessionId, `(() => {
    const row = [...document.querySelectorAll('.mcp-tool-table tbody tr')]
      .find((item) => item.querySelector('th span')?.textContent.trim() === 'controlled_policy');
    const button = row?.querySelector(${JSON.stringify(`[data-mcp-tool-${kind}]`)});
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('dialog[data-mcp-dialog=${kind}][open]')`
      + ` && document.querySelector('dialog[data-mcp-dialog=${kind}]').getAttribute('aria-busy') === 'false'`,
    `${stage}.ready`,
  );
}


async function verifySchemaAndClose(client, sessionId, stage) {
  const detail = await evaluate(client, sessionId, `(() => {
    const blocks = [...document.querySelectorAll('.mcp-schema-block pre code')];
    if (blocks.length !== 2) return false;
    try {
      const schemas = blocks.map((item) => JSON.parse(item.textContent));
      return schemas.every((schema) => schema?.type === 'object'
        && schema?.properties?.payload?.type === 'string'
        && schema?.additionalProperties === false);
    } catch { return false; }
  })()`, `${stage}.schema`);
  requireCondition(detail === true, stage, "schema");
  const closed = await evaluate(client, sessionId, `(() => {
    const dialog = document.querySelector('dialog[data-mcp-dialog=detail]');
    const button = dialog?.querySelector('[data-mcp-close-dialog]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, `${stage}.close`);
  requireCondition(closed === true, stage, "close");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-mcp-dialog], .mcp-tool-detail, .mcp-schema-block')",
    `${stage}.cleared`,
  );
  return true;
}


async function updatePolicy(client, sessionId, tracker, stage) {
  const before = tracker.policyStatuses.length;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-mcp-policy-form]');
    if (!form) return false;
    form.elements.namedItem('status').value = 'active';
    form.elements.namedItem('risk_level').value = 'high';
    form.elements.namedItem('approval_required').checked = true;
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForTrackerCount(
    () => tracker.policyStatuses.length,
    before + 1,
    `${stage}.network`,
    60_000,
  );
  requireCondition(tracker.policyStatuses.at(-1) === 200, stage, "status");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-mcp-dialog]')"
      + " && [...document.querySelectorAll('.mcp-tool-table tbody tr')]"
      + ".some((row) => row.querySelector('th span')?.textContent.trim() === 'controlled_policy'"
      + " && row.querySelector('td:nth-child(3) small')?.textContent.includes('policy r3'))",
    `${stage}.complete`,
  );
  return true;
}


async function exercisePage(client, sessionId, tracker, staging, privateConfig) {
  await setViewport(client, sessionId, VIEWPORTS.mobile);
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-mcp-root] .mcp-server-table tbody tr')",
    "browser.page",
  );
  const nativeDialog = await openRegistrationDialog(client, sessionId, "browser.registration.open");
  const registerShot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[3],
    VIEWPORTS.mobile,
    "dialog[data-mcp-dialog=register]",
    tracker,
    "browser.screenshot.register",
  );
  await submitRegistration(client, sessionId, tracker, privateConfig, "browser.registration.submit");

  await setViewport(client, sessionId, VIEWPORTS.desktop);
  await clickServerAction(client, sessionId, tracker, "status", 202, "browser.server.disable");
  await waitForDom(
    client,
    sessionId,
    "[...document.querySelectorAll('.mcp-server-table tbody tr')]"
      + ".some((row) => row.querySelector('th strong')?.textContent.trim() === 'Controlled Server'"
      + " && row.querySelector('[data-mcp-next-status=active]'))",
    "browser.server.disabled",
  );
  await clickServerAction(client, sessionId, tracker, "status", 202, "browser.server.enable");
  const dutiesSeparated = await evaluate(client, sessionId, `(() => {
    const row = [...document.querySelectorAll('.mcp-server-table tbody tr')]
      .find((item) => item.querySelector('th strong')?.textContent.trim() === 'Controlled Server');
    return Boolean(row?.querySelector('[data-mcp-server-status]'))
      && !row?.querySelector('[data-mcp-refresh-server]');
  })()`, "browser.server.operator_scope");
  requireCondition(dutiesSeparated === true, "browser.server.operator_scope", "permissions");

  await logout(client, sessionId, tracker, "browser.logout.server_operator");
  await login(
    client,
    sessionId,
    tracker,
    { email: ADMIN_EMAIL, password: privateConfig.adminPassword },
    "document.querySelector('#logout-button')",
    "browser.login.refresh_admin",
  );
  if (!await evaluate(
    client,
    sessionId,
    "Boolean(document.querySelector('[data-mcp-root]'))",
    "browser.refresh_admin.current",
  )) {
    await navigateTo(
      client,
      sessionId,
      "mcp-tools",
      "[data-mcp-root]",
      "browser.refresh_admin.navigate",
    );
  }
  await waitForDom(
    client,
    sessionId,
    "[...document.querySelectorAll('.mcp-server-table tbody tr')]"
      + ".some((row) => row.querySelector('th strong')?.textContent.trim() === 'Controlled Server'"
      + " && row.querySelector('[data-mcp-refresh-server]:not([disabled])'))",
    "browser.refresh_admin.ready",
  );
  await clickServerAction(client, sessionId, tracker, "refresh", 202, "browser.server.refresh");
  await refreshCurrentView(client, sessionId, tracker, "browser.list.servers");
  const servers = await evaluate(client, sessionId, `(() => ({
    rows: document.querySelectorAll('.mcp-server-table tbody tr').length,
    controlledActive: [...document.querySelectorAll('.mcp-server-table tbody tr')]
      .some((row) => row.querySelector('th strong')?.textContent.trim() === 'Controlled Server'
        && Boolean(row.querySelector('.tone-active'))
        && Boolean(row.querySelector('[data-mcp-next-status=disabled]'))),
  }))()`, "browser.server.final");
  requireCondition(servers.rows === 2 && servers.controlledActive, "browser.server.final", "facts");
  const serverShot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[0],
    VIEWPORTS.desktop,
    ".mcp-server-table",
    tracker,
    "browser.screenshot.servers",
  );

  await logout(client, sessionId, tracker, "browser.logout.refresh_admin");
  await login(
    client,
    sessionId,
    tracker,
    { email: POLICY_OPERATOR_EMAIL, password: privateConfig.rolePassword },
    "document.querySelector('#logout-button')",
    "browser.login.policy_operator",
  );
  if (!await evaluate(
    client,
    sessionId,
    "Boolean(document.querySelector('[data-mcp-root]'))",
    "browser.policy.current",
  )) {
    await navigateTo(client, sessionId, "mcp-tools", "[data-mcp-root]", "browser.policy.navigate");
  }
  await waitForDom(
    client,
    sessionId,
    "document.querySelectorAll('.mcp-server-table tbody tr').length === 2",
    "browser.policy.ready",
  );
  await switchTab(
    client,
    sessionId,
    "tools",
    "document.querySelectorAll('.mcp-tool-table tbody tr').length === 2",
    "browser.tab.tools",
  );
  await openToolDialog(client, sessionId, "detail", "browser.tool.detail");
  const schemaDetailVerified = await verifySchemaAndClose(client, sessionId, "browser.tool.detail");
  await openToolDialog(client, sessionId, "policy", "browser.tool.policy");
  const policyHighApproval = await updatePolicy(client, sessionId, tracker, "browser.tool.policy");
  await refreshCurrentView(client, sessionId, tracker, "browser.list.tools");
  const toolShot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[1],
    VIEWPORTS.desktop,
    ".mcp-tool-table",
    tracker,
    "browser.screenshot.tools",
  );

  await switchTab(
    client,
    sessionId,
    "invocations",
    "document.querySelectorAll('.mcp-invocation-row').length === 1",
    "browser.tab.invocations",
  );
  await refreshCurrentView(client, sessionId, tracker, "browser.list.invocations");
  const invocation = await evaluate(client, sessionId, `(() => {
    const row = document.querySelector('.mcp-invocation-row');
    const approval = row?.querySelector('.mcp-invocation-facts > div:nth-child(2) dd')
      ?.textContent.trim() || '';
    return {
      summary: Boolean(row?.querySelector('.tone-approval_pending'))
        && row.querySelectorAll('.mcp-invocation-facts > div').length === 11
        && !row.querySelector('pre,textarea,[data-mcp-arguments],[data-mcp-invocation-result]'),
      approval: approval.startsWith('apr_') && approval.length > 20,
    };
  })()`, "browser.invocation.summary");
  requireCondition(invocation.summary && invocation.approval, "browser.invocation", "summary");
  const invocationShot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[2],
    VIEWPORTS.mobile,
    ".mcp-invocation-row",
    tracker,
    "browser.screenshot.invocations",
  );

  return Object.freeze({
    desktop: combineShots(serverShot, toolShot),
    mobile: combineShots(invocationShot, registerShot),
    interaction: {
      serversVisible: servers.rows === 2,
      toolsVisible: true,
      registrationSubmitted: tracker.registrationStatuses.length === 1,
      refreshSubmitted: tracker.refreshStatuses.length === 1,
      serverDisabled: tracker.serverStatusValues[0] === "disabled",
      serverEnabled: tracker.serverStatusValues[1] === "active",
      schemaDetailVerified,
      policyHighApproval,
      invocationSummaryVisible: invocation.summary,
      approvalSummaryVisible: invocation.approval,
      registrationDialogNative: nativeDialog,
    },
  });
}


async function logoutWithLateResponse(client, sessionId, tracker) {
  const beforeAbort = tracker.abortEvents;
  const pausedPromise = pauseNextResponse(
    tracker,
    "invocation_list",
    200,
    "browser.logout.pause",
  );
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-mcp-refresh]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, "browser.logout.refresh");
  requireCondition(refreshed === true, "browser.logout.refresh", "button");
  await pausedPromise;
  await logout(client, sessionId, tracker, "browser.logout.operator");
  const cleared = await evaluate(client, sessionId, `(() => ({
    root: !document.querySelector('[data-mcp-root], .mcp-tools-panel'),
    storage: localStorage.length === 0 && sessionStorage.length === 0,
  }))()`, "browser.logout.cleared");
  const released = await releasePausedResponse(
    client,
    tracker,
    "browser.logout.release",
    { allowClosed: true },
  );
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const lateDiscarded = await evaluate(
    client,
    sessionId,
    "Boolean(document.querySelector('#login-form') && !document.querySelector('[data-mcp-root]'))",
    "browser.logout.late",
  );
  return Object.freeze({
    cleared: cleared.root && cleared.storage,
    abortObserved: !released || tracker.abortEvents > beforeAbort,
    lateDiscarded,
  });
}


async function verifyViewer(client, sessionId, tracker, privateConfig) {
  await login(
    client,
    sessionId,
    tracker,
    { email: VIEWER_EMAIL, password: privateConfig.rolePassword },
    "document.querySelector('#logout-button')",
    "browser.login.viewer",
  );
  await settle(client, sessionId, "browser.viewer.settle");
  const viewer = await evaluate(client, sessionId, `(() => ({
    navigationHidden: !document.querySelector('[data-console-nav="mcp-tools"]'),
    pageHidden: !document.querySelector('.mcp-tools-panel, [data-mcp-root]'),
  }))()`, "browser.viewer");
  requireCondition(viewer.navigationHidden && viewer.pageHidden, "browser.viewer", "visibility");
  await logout(client, sessionId, tracker, "browser.logout.viewer");
  return viewer;
}


async function revokeWithLateResponse(client, sessionId, tracker, privateConfig) {
  await login(
    client,
    sessionId,
    tracker,
    { email: OPERATOR_EMAIL, password: privateConfig.rolePassword },
    "document.querySelector('#logout-button')",
    "browser.login.operator_revoke",
  );
  if (!await evaluate(
    client,
    sessionId,
    "Boolean(document.querySelector('[data-mcp-root]'))",
    "browser.revoke.current",
  )) {
    await navigateTo(client, sessionId, "mcp-tools", "[data-mcp-root]", "browser.revoke.navigate");
  }
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('.mcp-server-table tbody tr')"
      + " && document.querySelector('[data-mcp-refresh]:not([disabled])')",
    "browser.revoke.ready",
  );
  const pausedPromise = pauseNextResponse(
    tracker,
    "server_list",
    200,
    "browser.revoke.pause",
  );
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-mcp-refresh]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, "browser.revoke.refresh");
  requireCondition(refreshed === true, "browser.revoke.refresh", "button");
  await pausedPromise;
  const revoked = await postHarness("/revoke-mcp", privateConfig, {});
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
    "!document.querySelector('[data-mcp-root], .mcp-tools-panel')"
      + " && !document.querySelector('[data-console-nav=mcp-tools]')",
    "browser.revoke.cleared",
  );
  const beforeRelease = await evaluate(client, sessionId, `(() => ({
    root: !document.querySelector('[data-mcp-root], .mcp-tools-panel'),
    navigation: !document.querySelector('[data-console-nav=mcp-tools]'),
  }))()`, "browser.revoke.before_release");
  await releasePausedResponse(client, tracker, "browser.revoke.release", { allowClosed: true });
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const lateDiscarded = await evaluate(
    client,
    sessionId,
    "!document.querySelector('[data-mcp-root], .mcp-tools-panel')",
    "browser.revoke.late",
  );
  await logout(client, sessionId, tracker, "browser.logout.revoked_operator");
  return Object.freeze({
    rootCleared: beforeRelease.root,
    navigationCleared: beforeRelease.navigation,
    lateDiscarded,
  });
}


async function verifyFinalPrivacy(client, sessionId, tracker) {
  const privacy = await evaluate(client, sessionId, `(async () => {
    const needles = ${JSON.stringify(tracker.privateValues)};
    const text = document.documentElement.textContent || '';
    const values = [...document.querySelectorAll('input,textarea,select')]
      .map((item) => String(item.value || ''));
    const attributes = [...document.querySelectorAll('*')]
      .flatMap((item) => [...item.attributes].map((attribute) => attribute.value));
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
  return true;
}


function requestContractsExact(tracker) {
  return tracker.requestContractsExact
    && tracker.requestContractCounts.registration === 1
    && tracker.requestContractCounts.refresh === 1
    && tracker.requestContractCounts.serverStatus === 2
    && tracker.requestContractCounts.policy === 1
    && JSON.stringify(tracker.serverStatusValues) === JSON.stringify(["disabled", "active"])
    && tracker.idempotencyKeys.size === 4;
}


function buildCompletionFacts({ page, logoutState, viewer, revocation, registration, tracker }) {
  return {
    status: "passed",
    desktop: {
      width: 1280,
      height: 720,
      nonblank: page.desktop.nonblank,
      page_114: true,
      servers_visible: page.interaction.serversVisible,
      tools_visible: page.interaction.toolsVisible,
      registration_submitted: page.interaction.registrationSubmitted,
      refresh_submitted: page.interaction.refreshSubmitted,
      server_disabled: page.interaction.serverDisabled,
      server_enabled: page.interaction.serverEnabled,
      schema_detail_verified: page.interaction.schemaDetailVerified,
      policy_high_approval: page.interaction.policyHighApproval,
      horizontal_overflow_free: page.desktop.layout.horizontalOverflow === false,
      text_overflow_free: page.desktop.layout.textOverflow === false,
      overlap_count: page.desktop.layout.overlapCount,
      no_sensitive_dom: page.desktop.noSensitiveDom,
      next_content_hint: page.desktop.nextContentHint,
    },
    mobile: {
      width: 390,
      height: 844,
      nonblank: page.mobile.nonblank,
      page_114: true,
      invocation_summary_visible: page.interaction.invocationSummaryVisible,
      approval_summary_visible: page.interaction.approvalSummaryVisible,
      registration_dialog_native: page.interaction.registrationDialogNative,
      horizontal_overflow_free: page.mobile.layout.horizontalOverflow === false,
      text_overflow_free: page.mobile.layout.textOverflow === false,
      overlap_count: page.mobile.layout.overlapCount,
      no_sensitive_dom: page.mobile.noSensitiveDom,
      next_content_hint: page.mobile.nextContentHint,
    },
    lifecycle: {
      backend_viewer_denied: true,
      viewer_navigation_hidden: viewer.navigationHidden,
      viewer_page_hidden: viewer.pageHidden,
      logout_cleared: logoutState.cleared,
      abort_observed: logoutState.abortObserved,
      logout_late_discarded: logoutState.lateDiscarded,
      revocation_root_cleared: revocation.rootCleared,
      revocation_navigation_cleared: revocation.navigationCleared,
      revocation_late_discarded: revocation.lateDiscarded,
      registered_private_values: registration.status === 200,
    },
    network: {
      external_requests: tracker.externalRequests,
      forbidden_queries: tracker.forbiddenQueries,
      console_errors: tracker.consoleErrors,
      page_errors: tracker.pageErrors,
      login_statuses: tracker.loginStatuses,
      logout_statuses: tracker.logoutStatuses,
      registration_statuses: tracker.registrationStatuses,
      refresh_statuses: tracker.refreshStatuses,
      server_statuses: tracker.serverStatuses,
      policy_statuses: tracker.policyStatuses,
      list_successes: tracker.listSuccesses,
      detail_successes: tracker.detailSuccesses,
      no_store_responses: tracker.noStoreResponses && tracker.noStoreObserved >= 12,
      request_contracts_exact: requestContractsExact(tracker),
    },
    screenshots: [...SCREENSHOTS],
    all_108_prds_complete: false,
  };
}


function validateCompletionFacts(value) {
  requireCondition(
    value?.status === "passed" && value?.all_108_prds_complete === false,
    "browser.complete",
    "status",
  );
  const trueSections = {
    desktop: [
      "nonblank", "page_114", "servers_visible", "tools_visible",
      "registration_submitted", "refresh_submitted", "server_disabled", "server_enabled",
      "schema_detail_verified", "policy_high_approval", "horizontal_overflow_free",
      "text_overflow_free", "no_sensitive_dom", "next_content_hint",
    ],
    mobile: [
      "nonblank", "page_114", "invocation_summary_visible", "approval_summary_visible",
      "registration_dialog_native", "horizontal_overflow_free", "text_overflow_free",
      "no_sensitive_dom", "next_content_hint",
    ],
    lifecycle: [
      "backend_viewer_denied", "viewer_navigation_hidden", "viewer_page_hidden",
      "logout_cleared", "abort_observed", "logout_late_discarded",
      "revocation_root_cleared", "revocation_navigation_cleared",
      "revocation_late_discarded", "registered_private_values",
    ],
  };
  for (const [section, names] of Object.entries(trueSections)) {
    requireCondition(
      names.every((name) => value?.[section]?.[name] === true),
      `browser.${section}`,
      "facts",
    );
  }
  requireCondition(
    value.desktop.width === 1280
      && value.desktop.height === 720
      && value.desktop.overlap_count === 0,
    "browser.desktop",
    "layout",
  );
  requireCondition(
    value.mobile.width === 390
      && value.mobile.height === 844
      && value.mobile.overlap_count === 0,
    "browser.mobile",
    "layout",
  );
  const network = value.network;
  requireCondition(
    network.external_requests === 0
      && network.forbidden_queries === 0
      && network.console_errors === 0
      && network.page_errors === 0,
    "browser.network",
    "errors",
  );
  requireCondition(
    JSON.stringify(network.login_statuses) === JSON.stringify([200, 200, 200, 200, 200])
      && JSON.stringify(network.logout_statuses) === JSON.stringify([200, 200, 200, 200, 200])
      && JSON.stringify(network.registration_statuses) === JSON.stringify([202])
      && JSON.stringify(network.refresh_statuses) === JSON.stringify([202])
      && JSON.stringify(network.server_statuses) === JSON.stringify([202, 202])
      && JSON.stringify(network.policy_statuses) === JSON.stringify([200]),
    "browser.network",
    "statuses",
  );
  requireCondition(
    network.list_successes >= 6
      && network.detail_successes >= 2
      && network.no_store_responses === true
      && network.request_contracts_exact === true,
    "browser.network",
    "contracts",
  );
  requireCondition(
    JSON.stringify(value.screenshots) === JSON.stringify(SCREENSHOTS),
    "browser.screenshots",
    "names",
  );
  return value;
}


async function run(runId) {
  requireCondition(
    existsSync(PYTHON) && existsSync(HARNESS) && existsSync(CHROME),
    "config.runtime",
    "missing",
  );
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
    await bindBrowserGuards(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      privateConfig,
    );
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
      { email: OPERATOR_EMAIL, password: privateConfig.rolePassword },
      "document.querySelector('[data-mcp-root]')",
      "browser.login.operator",
    );
    const page = await exercisePage(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      staging,
      privateConfig,
    );
    const logoutState = await logoutWithLateResponse(
      pageSession.client,
      pageSession.sessionId,
      tracker,
    );
    const viewer = await verifyViewer(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      privateConfig,
    );
    const revocation = await revokeWithLateResponse(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      privateConfig,
    );
    await verifyFinalPrivacy(pageSession.client, pageSession.sessionId, tracker);
    requireCondition(
      tracker.browserTokens.size === 5
        && tracker.idempotencyKeys.size === 4
        && tracker.processPrivacyViolations === 0
        && tracker.processPrivacyViolationCount === 0
        && tracker.edgeProcessErrorCount === 0,
      "browser.process",
      "privacy_or_error",
    );
    const registeredValues = [
      privateConfig.endpoint,
      privateConfig.bearer,
      privateConfig.driverOwner,
      ...tracker.browserTokens,
      ...tracker.idempotencyKeys,
    ];
    const registration = await postHarness("/register-private", privateConfig, {
      values: [...new Set(registeredValues)],
    });
    requireCondition(registration.status === 200, "harness.private", "status");
    const facts = buildCompletionFacts({
      page,
      logoutState,
      viewer,
      revocation,
      registration,
      tracker,
    });
    validateCompletionFacts(facts);

    await closeBrowser(browserClient, pageSession, browserChild);
    browserClient = null;
    pageSession = null;
    browserChild = null;
    await removeDriverDirectory(driverDirectory, privateConfig.driverOwner);
    const completed = await postHarness("/complete", privateConfig, facts);
    requireCondition(completed.status === 200, "harness.complete", "status");
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-082", screenshot_count: 4 });
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
  createDriverDirectory,
  directChild,
  parseArguments,
  requestContractsExact,
  safeRunId,
  terminateIdempotentBrowserOwner,
  validateCompletionFacts,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
