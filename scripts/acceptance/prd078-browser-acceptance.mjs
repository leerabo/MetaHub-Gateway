/**
 * PRD-078 PAGE-108/109 真实 Chromium 验收驱动。
 *
 * 驱动只启动仓库内 Python harness 与 Chrome，浏览器请求被限制到固定回环 Origin。
 * 角色密码、Bearer、step-up ticket 和审批 ID 只驻留进程/页面内存；最终完成信号只含
 * 布尔交互事实、布局计数和固定截图名，并始终保持 all_108_prds_complete=false。
 */

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, writeSync } from "node:fs";
import { lstat, mkdir, rm, writeFile } from "node:fs/promises";
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


// 验收器的仓库路径、固定回环端口、超时与输入格式均在此冻结，禁止环境变量扩大执行边界。
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd078-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd078-browser-driver");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd078-browser");
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18881";
const HARNESS_CONSOLE_URL = "http://127.0.0.1:5173/#approvals";
const DEBUG_PORT = 19078;
const DOM_WAIT_TIMEOUT_MS = 90_000;
const BACKGROUND_REFRESH_INTERVAL_MS = 15_000;
const BACKGROUND_REFRESH_GATE = "__prd078BackgroundRefreshGate";
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization)/i;
const HEALTH_ATTEMPT_REQUEST_ID = /^([A-Za-z0-9][A-Za-z0-9._:-]{0,239})_attempt_([12])$/;
const HEALTH_ROUTE_NAMES = Object.freeze({
  "/health": "health_summary",
  "/health/live": "health_live",
  "/health/ready": "health_ready",
  "/version": "health_version",
});
// 健康探测只接受 Gateway 与 Control 的两组固定回环 Origin 和显式路由类别。
const GATEWAY_ORIGINS = new Set(["http://localhost:8000", "http://127.0.0.1:8000"]);
const CONTROL_ORIGINS = new Set([CONTROL_ORIGIN, "http://127.0.0.1:8001"]);
const HEALTH_REQUEST_CATEGORIES = new Set([
  "gateway_health_summary",
  "gateway_health_live",
  "gateway_health_ready",
  "gateway_health_version",
  "control_health_summary",
  "control_health_live",
  "control_health_ready",
  "control_health_version",
]);
// Control 公共读取接口只用于诊断分类，分类结果从不替代服务端认证或权限判断。
const CONTROL_PUBLIC_GROUPS = new Set([
  "applications", "audit-logs", "budgets", "budget-status", "developer", "dlp",
  "environments", "model-prices", "permissions", "projects", "providers", "status",
  "tenant", "usage", "user", "users", "virtual-keys", "webhooks",
]);
const REQUEST_DIAGNOSTIC_CATEGORIES = new Set([
  "allowed", "approval_decision", "approval_detail", "approval_list", "approval_read",
  "approval_step_up", "auth_login", "auth_logout", "auth_me", "config_summary",
  "console_asset", "console_events", "console_navigation", "console_preferences",
  "control_other", "dashboard_role_view", "external", "gateway_other",
  ...[...CONTROL_PUBLIC_GROUPS].map((group) => `control_${group.replaceAll("-", "_")}`),
  ...HEALTH_REQUEST_CATEGORIES,
]);
// 角色、视口、截图和允许 Origin 都是冻结验收合同，任何运行时漂移必须失败关闭。
const EXPECTED_STEP_UP_CONSOLE_MESSAGES = new Set([
  "Failed to load resource: the server responded with a status of 403",
  "Failed to load resource: the server responded with a status of 403 ()",
  "Failed to load resource: the server responded with a status of 403 (Forbidden)",
]);
const ROLE_ACCOUNTS = Object.freeze({
  finance: Object.freeze({ email: "finance@prd078.runtime" }),
  security: Object.freeze({ email: "security@prd078.runtime" }),
});
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "finance-standard-modal-desktop-1280x720.png",
  "security-high-risk-modal-mobile-390x844.png",
  "security-high-risk-busy-mobile-390x844.png",
]);
const ALLOWED_ORIGINS = new Set([
  CONSOLE_ORIGIN,
  CONTROL_ORIGIN,
  "http://localhost:8000",
  "http://127.0.0.1:5173",
  "http://127.0.0.1:8001",
  "http://127.0.0.1:8000",
]);
// CORS owner 使用固定别名保存来源和目标身份，避免把完整 Origin 或动态 URL 写入证据。
const CONSOLE_ORIGINS = new Set([CONSOLE_ORIGIN, "http://127.0.0.1:5173"]);
const CONSOLE_ORIGIN_KEYS = new Map([
  [CONSOLE_ORIGIN, "console_localhost"],
  ["http://127.0.0.1:5173", "console_loopback"],
]);
const HEALTH_TARGET_ORIGIN_KEYS = new Map([
  ["http://localhost:8000", "gateway_localhost"],
  ["http://127.0.0.1:8000", "gateway_loopback"],
  [CONTROL_ORIGIN, "control_localhost"],
  ["http://127.0.0.1:8001", "control_loopback"],
]);
// 预检头集合和子进程 drain 状态是内存账本，不得包含凭据值或跨批次复用。
const STEP_UP_PREFLIGHT_HEADERS = Object.freeze(["authorization", "content-type"]);
const HEALTH_PREFLIGHT_HEADERS = Object.freeze(["x-request-id"]);
const CHILD_DRAIN_STATES = new WeakMap();


/** 所有错误都折叠为源码内固定坐标，禁止传播 URL、正文或底层异常。 */
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


/** 把未知异常折叠为固定安全坐标，只保留已验证的验收错误阶段和代码。 */
function safeFailure(error, stage = "browser.acceptance") {
  if (error instanceof BrowserAcceptanceFailure) return error;
  if (
    error?.name === "BrowserAcceptanceFailure"
    && SAFE_COORDINATE.test(String(error.stage ?? ""))
    && SAFE_COORDINATE.test(String(error.code ?? ""))
  ) return new BrowserAcceptanceFailure(error.stage, error.code);
  return new BrowserAcceptanceFailure(stage, "unexpected");
}


/** run-id 只能映射为两个固定仓库根的直属子目录。 */
function safeRunId(value) {
  const fallback = `prd078-${new Date().toISOString().replaceAll(/[-:.]/g, "")}`;
  const candidate = String(value || fallback);
  requireCondition(
    SAFE_RUN_ID.test(candidate) && !candidate.includes(".."),
    "config.run_id",
    "invalid",
  );
  return candidate;
}


/** 只解析唯一可选 run-id 参数，并把目录名约束交给统一安全校验。 */
function parseArguments(argv = process.argv.slice(2)) {
  let runId = "";
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] !== "--run-id" || runId || index + 1 >= argv.length) {
      throw new BrowserAcceptanceFailure("config.arguments", "invalid");
    }
    runId = argv[index + 1];
    index += 1;
  }
  return Object.freeze({ runId: safeRunId(runId) });
}


/** 将批次名解析为给定仓库根的直属子目录，拒绝遍历、绝对路径和多级目录。 */
function directChild(root, name, stage) {
  const target = path.resolve(root, name);
  requireCondition(
    path.dirname(target) === path.resolve(root) && path.basename(target) === name,
    stage,
    "outside_root",
  );
  return target;
}


/** 每轮角色密码与 harness 控制 Token 均为高熵一次性值，不从宿主环境继承。 */
function createPrivateConfiguration() {
  return Object.freeze({
    rolePassword: `Prd078-Role-${randomBytes(40).toString("base64url")}`,
    stopToken: `Prd078-Stop-${randomBytes(48).toString("base64url")}`,
  });
}


/** Python harness 只继承启动所需的系统坐标，不继承代理、GitHub 或云凭据。 */
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
    PRD078_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD078_BROWSER_STOP_TOKEN: privateConfig.stopToken,
  };
}


/** 在启动 Chrome 前验证调试端口可独占绑定，避免接入非本轮浏览器进程。 */
async function assertPortFree(port) {
  const free = await new Promise((resolve) => {
    const server = createServer();
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => server.close(() => resolve(true)));
  });
  requireCondition(free, "config.debug_port", "unavailable");
}


/** 创建本轮唯一浏览器 Profile 根；既有路径一律失败，绝不覆盖旧批次。 */
async function createDriverDirectory(runId) {
  await mkdir(DRIVER_ROOT, { recursive: true });
  const directory = directChild(DRIVER_ROOT, runId, "config.driver_directory");
  requireCondition(!existsSync(directory), "config.driver_directory", "exists");
  await mkdir(directory);
  return directory;
}


/** 只清理由本轮排他创建的直属普通目录，未知或链接路径保持原样。 */
async function removeDriverDirectory(directory) {
  if (!directory || !existsSync(directory)) return;
  requireCondition(
    path.dirname(path.resolve(directory)) === path.resolve(DRIVER_ROOT),
    "cleanup.driver_directory",
    "boundary",
  );
  const information = await lstat(directory);
  requireCondition(information.isDirectory() && !information.isSymbolicLink(), "cleanup.driver_directory", "type");
  await rm(directory, { recursive: true, force: false });
}


/** Harness stdout 只接受 exact ready JSON；stderr 不回显，只解析固定失败坐标。 */
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
      const match = stderrBuffer.match(/PRD-078 browser harness failed: ([a-z][a-z0-9_.-]{1,100}):([a-z][a-z0-9_.-]{1,100})/);
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


/** 只检查子进程输出是否命中本轮私密值，不把输出正文保存到 tracker 或证据。 */
function inspectProcessChunk(chunk, tracker) {
  const text = String(chunk ?? "");
  if (tracker.privateValues.some((value) => value && text.includes(value))) {
    tracker.processPrivacyViolations += 1;
  }
}


/** 使用一次性控制 Token 调用固定 harness 路径，网络异常只返回脱敏坐标。 */
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


/** 等待 harness 与继承句柄完整退出，超时或非零状态均拒绝发布证据。 */
async function waitForHarnessClose(harness, timeoutMs = 180_000) {
  let timer;
  let code;
  try {
    code = await Promise.race([
      harness.closed,
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new BrowserAcceptanceFailure("harness.close", "timeout")),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
  requireCondition(code === 0, "harness.close", "exit");
}


/** 失败路径先请求 harness 自有清理，逾时才回收本 runner 创建的精确进程树。 */
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


/** 创建单批次内存账本；只保存固定分类、计数和 owner，不持久化凭据或响应正文。 */
function createTracker(privateConfig) {
  const privateValues = [
    privateConfig.rolePassword,
    privateConfig.stopToken,
    ROLE_ACCOUNTS.finance.email,
    ROLE_ACCOUNTS.security.email,
  ];
  return {
    privateValues,
    // Shared launchEdge 读取这三个固定字段；本驱动在最终门禁中一并核对。
    secrets: privateValues,
    processPrivacyViolationCount: 0,
    edgeProcessErrorCount: 0,
    browserTokens: new Set(),
    requestOwners: new Map(),
    fetchStepUpOwners: new Map(),
    stepUpPreflightOwners: new Map(),
    healthPreflightOwners: new Map(),
    expectedCanceledRequestIds: new Set(),
    seenResponses: new Set(),
    loginStatuses: [],
    logoutStatuses: [],
    listStatuses: [],
    detailStatuses: [],
    stepUpStatuses: [],
    interceptedStepUpStatuses: [],
    decisionStatuses: [],
    externalRequests: 0,
    forbiddenQueries: 0,
    requestPrivacyViolations: 0,
    processPrivacyViolations: 0,
    unexpectedConsoleErrors: 0,
    unexpectedConsoleReasons: new Map(),
    consoleNetworkFailureCount: 0,
    expectedStepUpConsoleErrors: 0,
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
    runtimeExceptions: 0,
    browserProcessErrors: 0,
    networkActivityRevision: 0,
    pausedResponse: null,
    pauseExpectation: null,
    phase: "startup",
  };
}


/**
 * 在验收 Page 内冻结唯一的 15 秒全壳后台刷新；其他计时器仍委托给浏览器原生实现。
 *
 * 真实审批流程全部由显式导航和命令驱动。冻结无关周期任务可避免它在注销边界启动一轮长刷新，随后因
 * 页面销毁制造 ERR_FAILED；捕获数量会在页面加载后精确校验，产品源码不包含任何验收分支。
 */
function installBackgroundRefreshClockGate(
  target,
  gateName = BACKGROUND_REFRESH_GATE,
  intervalMs = BACKGROUND_REFRESH_INTERVAL_MS,
) {
  if (!target || typeof target.setInterval !== "function" || target[gateName]) {
    return false;
  }
  const original = target.setInterval;
  const state = { capturedCount: 0, original, wrapper: null };
  const wrapper = function wrappedSetInterval(callback, delay, ...args) {
    if (delay === intervalMs) {
      state.capturedCount += 1;
      return 0;
    }
    return Reflect.apply(original, this, [callback, delay, ...args]);
  };
  state.wrapper = wrapper;
  Object.defineProperty(target, gateName, {
    configurable: true,
    enumerable: false,
    value: state,
  });
  target.setInterval = wrapper;
  return true;
}


/** 恢复原生计时器并删除短生命周期验收状态，只返回固定计数和布尔事实。 */
function restoreBackgroundRefreshClockGate(target, gateName = BACKGROUND_REFRESH_GATE) {
  const state = target?.[gateName];
  if (!state) return Object.freeze({ capturedCount: -1, restored: false });
  const ownsWrapper = target.setInterval === state.wrapper;
  if (ownsWrapper) target.setInterval = state.original;
  const facts = Object.freeze({
    capturedCount: Number(state.capturedCount ?? -1),
    restored: ownsWrapper && target.setInterval === state.original,
  });
  delete target[gateName];
  return facts;
}


/** 校验后台刷新只被捕获一次且原生计时器已恢复，防止验收分支残留页面。 */
function validateBackgroundRefreshClockGateFacts(facts) {
  requireCondition(facts?.capturedCount === 1, "browser.clock", "background_refresh_count");
  requireCondition(facts?.restored === true, "browser.clock", "restore");
  return true;
}


/** 首次导航前把纯函数注入主世界；页面加载后立即移除 future-document 注册并恢复原生计时器。 */
async function installBrowserClockGate(client, sessionId) {
  const source = `(${installBackgroundRefreshClockGate.toString()})(window, ${JSON.stringify(
    BACKGROUND_REFRESH_GATE,
  )}, ${BACKGROUND_REFRESH_INTERVAL_MS});`;
  const result = await client.send(
    "Page.addScriptToEvaluateOnNewDocument",
    { source },
    sessionId,
  );
  requireCondition(typeof result?.identifier === "string" && result.identifier, "browser.clock", "install");
  return result.identifier;
}


/** 恢复页面原生计时器并移除 future-document 注入，失败时也执行注册清理。 */
async function restoreBrowserClockGate(client, sessionId, identifier) {
  let facts;
  try {
    facts = await evaluate(
      client,
      sessionId,
      `(${restoreBackgroundRefreshClockGate.toString()})(window, ${JSON.stringify(
        BACKGROUND_REFRESH_GATE,
      )})`,
      "browser.clock.restore",
    );
  } finally {
    await client.send("Page.removeScriptToEvaluateOnNewDocument", { identifier }, sessionId);
  }
  validateBackgroundRefreshClockGateFacts(facts);
  return true;
}


/** 网络请求开始或结束都推进 revision，覆盖一次轮询间隙内快速完成的短请求。 */
function recordNetworkActivity(tracker) {
  tracker.networkActivityRevision = Number(tracker.networkActivityRevision ?? 0) + 1;
}


function readHeader(headers, name) {
  const expected = name.toLowerCase();
  for (const [key, value] of Object.entries(headers ?? {})) {
    if (key.toLowerCase() === expected) return String(value ?? "");
  }
  return "";
}


/** 只豁免撤权故障注入对应的 Chromium 固定 403 资源错误；其他来源和正文一律失败。 */
function classifyBrowserConsoleError({ type = "", text = "", url = "", argumentCount = -1 } = {}) {
  if (!["error", "assert"].includes(String(type))) return "ignored";
  let target;
  try { target = new URL(String(url)); } catch { target = null; }
  if (
    argumentCount === 0
    && EXPECTED_STEP_UP_CONSOLE_MESSAGES.has(String(text))
    && target?.origin === CONTROL_ORIGIN
    && /^\/approvals\/[^/]+\/step-up$/.test(target.pathname)
    && target.search === ""
  ) return "expected_step_up_403";
  return "unexpected_error";
}


/** 把未豁免控制台错误折叠为固定枚举；正文、URL 和动态标识均不得进入 tracker。 */
function classifyUnexpectedConsoleReason({ type = "", text = "", source = "log" } = {}) {
  if (String(type) === "assert") return "runtime_assert";
  const value = String(text ?? "");
  const http = value.match(
    /^Failed to load resource: the server responded with a status of ([1-5][0-9]{2})(?: \([^\r\n)]{0,80}\))?$/,
  );
  if (http) {
    const status = Number(http[1]);
    if (status >= 400 && status < 500) return "http_4xx";
    if (status >= 500) return "http_5xx";
    return "http_other";
  }
  const network = value.match(/^Failed to load resource: net::(ERR_[A-Z_]+)$/i)?.[1]?.toUpperCase();
  if (network === "ERR_FAILED") return "net_failed";
  if (network === "ERR_ABORTED") return "net_aborted";
  if (network === "ERR_BLOCKED_BY_CLIENT") return "net_blocked";
  if (network === "ERR_CONNECTION_REFUSED") return "net_refused";
  if (network) return "net_other";
  if (/Failed to fetch/i.test(value)) return "fetch_failed";
  return source === "runtime" ? "runtime_other" : "log_other";
}


/** 控制台 URL 只折叠为固定路由类别；动态审批 ID 和完整 URL 从不进入诊断坐标。 */
function classifyConsoleUrl(url) {
  let target;
  try { target = new URL(String(url)); } catch { return "no_url"; }
  if (target.origin === CONSOLE_ORIGIN || target.origin === "http://127.0.0.1:5173") {
    return "console";
  }
  const healthCategory = classifyHealthRequest(target, "GET");
  if (healthCategory) return healthCategory;
  if (GATEWAY_ORIGINS.has(target.origin)) return "gateway_other";
  if (!CONTROL_ORIGINS.has(target.origin)) {
    return "external";
  }
  if (/^\/approvals\/[^/]+\/step-up$/.test(target.pathname)) return "approval_step_up";
  if (target.pathname === "/approvals" || /^\/approvals\/[^/]+$/.test(target.pathname)) {
    return "approval_read";
  }
  if (/^\/approvals\/[^/]+\/(?:approve|reject)$/.test(target.pathname)) {
    return "approval_decision";
  }
  // 认证与会话路由只折叠为固定类别，URL、用户标识和任何 Token 都不得进入诊断坐标。
  if (target.pathname === "/auth/login") return "auth_login";
  if (target.pathname === "/auth/logout") return "auth_logout";
  if (target.pathname === "/auth/me") return "auth_me";
  if (target.pathname === "/console/navigation") return "console_navigation";
  if (target.pathname === "/console/preferences") return "console_preferences";
  if (target.pathname === "/console/events") return "console_events";
  // 首页角色视图与配置摘要同样只返回源码内枚举，不能携带响应正文或动态路径。
  if (target.pathname === "/dashboard/role-view") return "dashboard_role_view";
  if (target.pathname === "/internal/config/summary") return "config_summary";
  const publicGroup = target.pathname.split("/").filter(Boolean)[0] ?? "";
  if (CONTROL_PUBLIC_GROUPS.has(publicGroup)) return `control_${publicGroup.replaceAll("-", "_")}`;
  return "control_other";
}


/** 精确识别 Gateway/Control 的四条只读健康路由；query 或非 GET 均不参与恢复。 */
function classifyHealthRequest(targetOrUrl, method = "GET") {
  let target = targetOrUrl;
  if (!(target instanceof URL)) {
    try { target = new URL(String(targetOrUrl)); } catch { return null; }
  }
  if (String(method).toUpperCase() !== "GET" || target.search !== "") return null;
  const route = HEALTH_ROUTE_NAMES[target.pathname];
  // 未登记路由不得伪装成可恢复健康请求，避免宽松前缀匹配掩盖异常请求。
  if (!route) return null;
  // 服务类别必须来自固定回环 Origin，外部或别名漂移请求一律不进入健康账本。
  if (GATEWAY_ORIGINS.has(target.origin)) return `gateway_${route}`;
  if (CONTROL_ORIGINS.has(target.origin)) return `control_${route}`;
  return null;
}


/** 路由分类只返回固定枚举，审批 ID 从不进入追踪器或证据。 */
function classifyRequest(url, method = "GET") {
  let target;
  try { target = new URL(url); } catch { return "external"; }
  const normalizedMethod = String(method).toUpperCase();
  const healthCategory = classifyHealthRequest(target, normalizedMethod);
  if (healthCategory) return healthCategory;
  const healthPreflight = normalizedMethod === "OPTIONS"
    ? exactHealthPreflightRoute(target)
    : null;
  if (healthPreflight) return healthPreflight;
  if (GATEWAY_ORIGINS.has(target.origin)) return "gateway_other";
  if (!CONTROL_ORIGINS.has(target.origin)) {
    return target.origin === CONSOLE_ORIGIN || target.origin === "http://127.0.0.1:5173"
      ? "console_asset"
      : ALLOWED_ORIGINS.has(target.origin) ? "allowed" : "external";
  }
  // 认证 POST 只按精确方法和路径分类；请求正文、账号和 Token 均不参与类别生成。
  if (target.pathname === "/auth/login" && normalizedMethod === "POST") return "auth_login";
  if (target.pathname === "/auth/logout" && normalizedMethod === "POST") return "auth_logout";
  if (target.pathname === "/approvals" && normalizedMethod === "GET") return "approval_list";
  if (/^\/approvals\/[^/]+$/.test(target.pathname) && normalizedMethod === "GET") return "approval_detail";
  if (/^\/approvals\/[^/]+\/step-up$/.test(target.pathname) && normalizedMethod === "POST") return "approval_step_up";
  if (/^\/approvals\/[^/]+\/(?:approve|reject)$/.test(target.pathname) && normalizedMethod === "POST") return "approval_decision";
  // Control 的非 GET/OPTIONS 健康路径不得经 methodless Console 分类重新伪装为健康探测。
  if (healthPreflightCategory(target)) return "control_other";
  return classifyConsoleUrl(target.href);
}


function browserRequestAllowed(url) {
  try {
    const target = new URL(url);
    return ["data:", "blob:", "about:"].includes(target.protocol)
      || (ALLOWED_ORIGINS.has(target.origin)
        && !target.username
        && !target.password
        && ![...target.searchParams.keys()].some((key) => FORBIDDEN_QUERY_KEY.test(key)));
  } catch {
    return false;
  }
}


/** 按唯一 CDP request id 记录固定业务类别状态，重复响应不得扩大计数。 */
function recordResponse(tracker, requestId, status, category) {
  if (!requestId || tracker.seenResponses.has(requestId)) return;
  tracker.seenResponses.add(requestId);
  // 登录与注销只登记数值状态，响应 Header、正文和 Session Token 不进入验收账本。
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category === "approval_list") tracker.listStatuses.push(status);
  if (category === "approval_detail") tracker.detailStatuses.push(status);
  if (category === "approval_step_up") tracker.stepUpStatuses.push(status);
  if (category === "approval_decision") tracker.decisionStatuses.push(status);
}


/** 健康探测 request_id 只接受同一 logical owner 的 attempt 1/2 固定格式。 */
function parseHealthAttemptRequestId(value) {
  const match = String(value ?? "").match(HEALTH_ATTEMPT_REQUEST_ID);
  return match
    ? Object.freeze({ logicalId: match[1], attempt: Number(match[2]) })
    : null;
}


/** 把加载失败折叠为固定枚举，原始错误文本和 URL 不进入 tracker 或证据。 */
function classifyNetworkFailure(errorText) {
  const value = String(errorText ?? "");
  if (/(?:^|:)ERR_ABORTED(?:$|\b)/i.test(value)) return "aborted";
  if (/(?:^|:)ERR_FAILED(?:$|\b)/i.test(value)) return "failed";
  if (/ERR_CONNECTION_REFUSED/i.test(value)) return "connection_refused";
  if (/ERR_BLOCKED_BY_CLIENT/i.test(value)) return "blocked";
  if (/ERR_NAME_NOT_RESOLVED/i.test(value)) return "dns";
  if (/ERR_INTERNET_DISCONNECTED/i.test(value)) return "disconnected";
  return "other";
}


/** 把网络异常压缩为固定 phase/category/reason，禁止 URL、Header 或底层错误原文入账。 */
function recordUnexpectedNetworkFailure(tracker, owner, reason) {
  tracker.unexpectedNetworkFailureCount = Number(tracker.unexpectedNetworkFailureCount ?? 0) + 1;
  if (!tracker.unexpectedNetworkFailure) {
    const phase = SAFE_COORDINATE.test(String(owner?.phase ?? "")) ? String(owner.phase) : "unknown";
    const category = REQUEST_DIAGNOSTIC_CATEGORIES.has(owner?.category)
      ? owner.category
      : "unowned";
    tracker.unexpectedNetworkFailure = Object.freeze({ phase, category, reason });
  }
}


/**
 * 为一次真实表单提交创建只含固定角色、阶段和递增序号的登录 owner；凭据、Token 和响应正文均不入账。
 */
function beginLoginAttempt(tracker, role, stage) {
  tracker.loginAttempts ??= [];
  tracker.loginRequestProofs ??= new Map();
  const previous = tracker.activeLoginAttempt;
  if (previous && !["completed", "invalid"].includes(previous.state)) {
    recordUnexpectedNetworkFailure(
      tracker,
      { category: "auth_login", phase: String(tracker.phase || "unknown") },
      "login_attempt_overlap",
    );
    return previous;
  }
  const sequence = Number(tracker.loginAttemptSequence ?? 0) + 1;
  tracker.loginAttemptSequence = sequence;
  const attempt = {
    sequence,
    role: ["finance", "security"].includes(role) ? role : "unknown",
    stage: SAFE_COORDINATE.test(String(stage ?? "")) ? String(stage) : "browser.login.unknown",
    phase: SAFE_COORDINATE.test(String(tracker.phase ?? "")) ? String(tracker.phase) : "unknown",
    state: "await_login",
    login: { requestId: null, responseStatus: null, terminal: null },
    me: { requestId: null, responseStatus: null, terminal: null },
  };
  tracker.loginAttempts.push(attempt);
  tracker.activeLoginAttempt = attempt;
  recordNetworkActivity(tracker);
  return attempt;
}


/** 登录链故障只登记固定 phase/category/reason，并把本 attempt 永久关闭为 invalid。 */
function failLoginAttempt(tracker, attempt, category, reason) {
  if (attempt) attempt.state = "invalid";
  recordUnexpectedNetworkFailure(tracker, {
    category: category === "auth_me" ? "auth_me" : "auth_login",
    phase: String(attempt?.phase ?? tracker.phase ?? "unknown"),
  }, reason);
  return "invalid";
}


/** 两段请求都必须同时具有 200 response 与 loadingFinished，且顺序只能是 login 后首个 auth/me。 */
function reconcileLoginAttempt(tracker, attempt) {
  if (!attempt || attempt.state === "invalid") return "invalid";
  const loginComplete = attempt.login.requestId
    && attempt.login.responseStatus === 200
    && attempt.login.terminal === "finished";
  if (loginComplete && attempt.state === "await_login") attempt.state = "await_me";
  const meComplete = attempt.me.requestId
    && attempt.me.responseStatus === 200
    && attempt.me.terminal === "finished";
  if (loginComplete && meComplete) attempt.state = "completed";
  return attempt.state;
}


/** 登录证明只接受无凭据、query、fragment 的固定 Control auth 路由。 */
function exactAuthRoute(url, pathname) {
  let target;
  try { target = new URL(String(url)); } catch { return false; }
  return target.origin === CONTROL_ORIGIN
    && target.pathname === pathname
    && !target.username
    && !target.password
    && target.search === ""
    && target.hash === "";
}


/**
 * 把 Network.requestWillBeSent 精确绑定到当前 loginRole owner。完成后的普通刷新 /auth/me 不属于登录证明。
 */
function registerLoginNetworkRequest(tracker, event, owner) {
  const requestId = String(event?.requestId ?? "");
  const previousProof = tracker.loginRequestProofs?.get(requestId);
  if (previousProof) {
    failLoginAttempt(
      tracker,
      previousProof.attempt,
      previousProof.category,
      event?.redirectResponse ? "login_redirect" : "login_request_id_reuse",
    );
    return "invalid";
  }
  const category = owner?.category;
  // 登录证明只接受两条固定业务类别；其他认证刷新或预检由通用网络 owner 负责收敛。
  if (!["auth_login", "auth_me"].includes(category)) return "ignored";
  // CORS OPTIONS 仍由通用 Network owner 收敛；登录业务证明只绑定精确 POST/GET。
  if (owner.method === "OPTIONS") return "ignored";
  const attempt = tracker.activeLoginAttempt;
  if (!attempt || ["completed", "invalid"].includes(attempt.state)) return "ignored";
  if (event?.redirectResponse) {
    return failLoginAttempt(tracker, attempt, category, "login_redirect");
  }
  // 没有 CDP request id 就无法绑定本次提交，必须立即关闭 attempt 而不是等待 DOM 超时。
  if (!requestId) return failLoginAttempt(tracker, attempt, category, "login_request_id");
  if (tracker.requestOwners?.has(requestId)) {
    return failLoginAttempt(tracker, attempt, category, "login_request_id_reuse");
  }
  if (String(tracker.phase || "unknown") !== attempt.phase || owner.phase !== attempt.phase) {
    return failLoginAttempt(tracker, attempt, category, "login_phase");
  }
  const leg = category === "auth_login" ? attempt.login : attempt.me;
  const expectedMethod = category === "auth_login" ? "POST" : "GET";
  const expectedPath = category === "auth_login" ? "/auth/login" : "/auth/me";
  // 实际 URL 必须精确命中当前 leg，任何 query、凭据、redirect 或路由漂移都不能作为登录证据。
  if (!exactAuthRoute(event?.request?.url, expectedPath)) {
    return failLoginAttempt(tracker, attempt, category, "login_route");
  }
  if (owner.method !== expectedMethod) {
    return failLoginAttempt(tracker, attempt, category, "login_method");
  }
  // POST 登录必须先完成才能接纳首个 /auth/me；重复或反序请求固定使 owner 失效。
  if (category === "auth_login") {
    if (attempt.state !== "await_login" || leg.requestId) {
      return failLoginAttempt(tracker, attempt, category, "login_duplicate_request");
    }
  } else if (attempt.state !== "await_me" || leg.requestId) {
    return failLoginAttempt(
      tracker,
      attempt,
      category,
      leg.requestId ? "login_duplicate_request" : "login_sequence",
    );
  }
  leg.requestId = requestId;
  tracker.loginRequestProofs.set(requestId, Object.freeze({ attempt, category, leg }));
  return "registered";
}


/** 登录 response 只接受同一 owner 的唯一 200；先于 terminal 或晚于 terminal 均可收敛。 */
function recordLoginNetworkResponse(tracker, requestId, status) {
  const proof = tracker.loginRequestProofs?.get(requestId);
  if (!proof) return "unowned";
  const { attempt, category, leg } = proof;
  if (String(tracker.phase || "unknown") !== attempt.phase) {
    return failLoginAttempt(tracker, attempt, category, "login_phase");
  }
  if (leg.responseStatus !== null) {
    return failLoginAttempt(tracker, attempt, category, "login_duplicate_response");
  }
  leg.responseStatus = status;
  if (status !== 200) return failLoginAttempt(tracker, attempt, category, "login_status");
  reconcileLoginAttempt(tracker, attempt);
  return leg.terminal === "finished" ? "login_response_settled" : "login_response_waiting_terminal";
}


/** 登录 terminal 必须唯一；任意 loadingFailed 都立即关闭 attempt，不保留底层错误文本。 */
function recordLoginNetworkTerminal(tracker, requestId, terminal) {
  const proof = tracker.loginRequestProofs?.get(requestId);
  if (!proof) return "unowned";
  const { attempt, category, leg } = proof;
  if (String(tracker.phase || "unknown") !== attempt.phase) {
    return failLoginAttempt(tracker, attempt, category, "login_phase");
  }
  if (leg.terminal !== null) {
    return failLoginAttempt(tracker, attempt, category, "login_duplicate_terminal");
  }
  leg.terminal = terminal;
  if (terminal !== "finished") {
    return failLoginAttempt(tracker, attempt, category, "login_loading_failed");
  }
  reconcileLoginAttempt(tracker, attempt);
  return leg.responseStatus === 200 ? "login_terminal_settled" : "login_terminal_waiting_response";
}


/** 超时诊断只返回固定缺口坐标，绝不读取响应正文或登录页错误文案。 */
function assertLoginAttemptComplete(attempt, stage = "browser.login") {
  requireCondition(Boolean(attempt?.login?.requestId), stage, "login_request_missing");
  requireCondition(attempt.login.responseStatus === 200, stage, "login_response_missing");
  requireCondition(attempt.login.terminal === "finished", stage, "login_terminal_missing");
  requireCondition(Boolean(attempt?.me?.requestId), stage, "auth_me_request_missing");
  requireCondition(attempt.me.responseStatus === 200, stage, "auth_me_response_missing");
  requireCondition(attempt.me.terminal === "finished", stage, "auth_me_terminal_missing");
  requireCondition(attempt.state === "completed", stage, "login_incomplete");
  return true;
}


/** 比较 Fetch 与 Network 的 step-up 业务 owner，只接受同阶段的固定 POST 身份。 */
function stepUpOwnersMatch(left, right) {
  return left?.category === "approval_step_up"
    && right?.category === "approval_step_up"
    && left.method === "POST"
    && right.method === "POST"
    && left.phase === right.phase;
}


/** 比较两条 step-up 预检 owner，确保方法、类别和验收阶段完全一致。 */
function stepUpPreflightOwnersMatch(left, right) {
  return left?.category === "approval_step_up"
    && right?.category === "approval_step_up"
    && left.method === "OPTIONS"
    && right.method === "OPTIONS"
    && left.phase === right.phase;
}


/** 比较健康预检 owner，并绑定固定服务路由、目标别名和 Console 来源别名。 */
function healthPreflightOwnersMatch(left, right) {
  return HEALTH_REQUEST_CATEGORIES.has(left?.category)
    && left?.category === right?.category
    && left.method === "OPTIONS"
    && right.method === "OPTIONS"
    && left.phase === right.phase
    && left.healthTargetKey === right.healthTargetKey
    && left.consoleOriginKey === right.consoleOriginKey;
}


/** 返回固定健康路由类别；query、凭据等合法性由 exactHealthPreflightRoute 单独判定。 */
function healthPreflightCategory(targetOrUrl) {
  let target = targetOrUrl;
  if (!(target instanceof URL)) {
    try { target = new URL(String(targetOrUrl)); } catch { return null; }
  }
  const route = HEALTH_ROUTE_NAMES[target.pathname];
  // 未登记健康路由不产生预检 owner，避免把任意 Control/Gateway URL 纳入恢复证据。
  if (!route) return null;
  // 只有固定回环服务 Origin 可以形成健康类别，外部请求始终留在网络防火墙处理。
  if (GATEWAY_ORIGINS.has(target.origin)) return `gateway_${route}`;
  if (CONTROL_ORIGINS.has(target.origin)) return `control_${route}`;
  return null;
}


/** 健康预检只允许固定回环 Origin 上无凭据、query、fragment 的四条健康路由。 */
function exactHealthPreflightRoute(targetOrUrl) {
  let target = targetOrUrl;
  if (!(target instanceof URL)) {
    try { target = new URL(String(targetOrUrl)); } catch { return null; }
  }
  const category = healthPreflightCategory(target);
  return category
    && !target.username
    && !target.password
    && target.search === ""
    && target.hash === ""
    ? category
    : null;
}


/** 只返回源码内固定键，避免把 URL 或 Origin 原文带入 tracker 与失败证据。 */
function healthPreflightIdentity(url, headers) {
  let target;
  try { target = new URL(String(url)); } catch { return null; }
  const category = exactHealthPreflightRoute(target);
  const targetOriginKey = HEALTH_TARGET_ORIGIN_KEYS.get(target.origin) ?? null;
  if (!category || !targetOriginKey) return null;
  return Object.freeze({
    category,
    healthTargetKey: `${targetOriginKey}.${category}`,
    consoleOriginKey: CONSOLE_ORIGIN_KEYS.get(readHeader(headers, "Origin")) ?? null,
  });
}


/** 拆分 CORS 逗号列表并规范化 token；调用方只比较白名单，不保存 Header 原文。 */
function commaSeparatedHeaderTokens(value, normalize = (item) => item) {
  return String(value ?? "")
    .split(",")
    .map((item) => normalize(item.trim()))
    .filter(Boolean);
}


/** 证明浏览器实际收到足以放行该 GET + X-Request-ID 的 CORS 响应合同。 */
function healthPreflightResponseAllowed(owner, headers) {
  const expectedOrigin = owner?.consoleOriginKey === "console_localhost"
    ? CONSOLE_ORIGIN
    : owner?.consoleOriginKey === "console_loopback"
      ? "http://127.0.0.1:5173"
      : null;
  if (!expectedOrigin || readHeader(headers, "Access-Control-Allow-Origin") !== expectedOrigin) {
    return false;
  }
  const methods = commaSeparatedHeaderTokens(
    readHeader(headers, "Access-Control-Allow-Methods"),
    (item) => item.toUpperCase(),
  );
  const allowedHeaders = commaSeparatedHeaderTokens(
    readHeader(headers, "Access-Control-Allow-Headers"),
    (item) => item.toLowerCase(),
  );
  return methods.includes("GET") && allowedHeaders.includes("x-request-id");
}


/** 精确识别健康 GET 因 X-Request-ID 触发的 CORS 预检，不保存任何 Header 原文。 */
function classifyHealthPreflight(event) {
  const method = String(event?.request?.method ?? "GET").toUpperCase();
  if (method !== "OPTIONS") return "not_preflight";
  const category = healthPreflightCategory(event?.request?.url);
  if (!category) return "not_preflight";
  // 宽松类别命中后仍须拒绝凭据、query、fragment 或 Origin 漂移，不能生成模糊预检证明。
  if (exactHealthPreflightRoute(event?.request?.url) !== category) return "invalid";
  const headers = event?.request?.headers ?? {};
  const identity = healthPreflightIdentity(event?.request?.url, headers);
  const origin = readHeader(headers, "Origin");
  const requestedMethod = readHeader(headers, "Access-Control-Request-Method").toUpperCase();
  const requestedHeaders = readHeader(headers, "Access-Control-Request-Headers")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .sort();
  return identity?.category === category
    && CONSOLE_ORIGINS.has(origin)
    && requestedMethod === "GET"
    && JSON.stringify(requestedHeaders) === JSON.stringify(HEALTH_PREFLIGHT_HEADERS)
    ? "valid"
    : "invalid";
}


/** Fetch request-stage 验证全部预检输入；Network 事件随后以 networkId 完成响应和终态对账。 */
function registerHealthPreflightRequest(tracker, event) {
  const classification = classifyHealthPreflight(event);
  if (classification === "not_preflight") return "ignored";
  const requestId = String(event?.requestId ?? "");
  const networkId = String(event?.networkId ?? "");
  const identity = healthPreflightIdentity(event?.request?.url, event?.request?.headers);
  const owner = Object.freeze({
    category: healthPreflightCategory(event?.request?.url),
    method: "OPTIONS",
    phase: String(tracker.phase || "unknown"),
    healthTargetKey: identity?.healthTargetKey ?? null,
    consoleOriginKey: identity?.consoleOriginKey ?? null,
    networkResponseStatus: null,
    corsResponseAllowed: false,
  });
  if (classification !== "valid") {
    recordUnexpectedNetworkFailure(tracker, owner, "health_preflight");
    return "invalid";
  }
  if (!requestId || !networkId) {
    recordUnexpectedNetworkFailure(tracker, owner, "health_preflight_network_id");
    return "invalid";
  }
  tracker.healthPreflightOwners ??= new Map();
  if (tracker.healthPreflightOwners.has(networkId)) {
    recordUnexpectedNetworkFailure(tracker, owner, "health_preflight_duplicate");
    return "invalid";
  }
  const networkOwner = tracker.requestOwners.get(networkId);
  if (networkOwner && !healthPreflightOwnersMatch(networkOwner, owner)) {
    recordUnexpectedNetworkFailure(tracker, owner, "health_preflight_owner_mismatch");
    return "invalid";
  }
  tracker.healthPreflightOwners.set(networkId, owner);
  recordNetworkActivity(tracker);
  return owner;
}


/** 精确验证无凭据、query 或 fragment 的 Control step-up 路由，拒绝任意目标地址漂移。 */
function exactStepUpRoute(url) {
  let target;
  try { target = new URL(String(url)); } catch { return false; }
  return target.origin === CONTROL_ORIGIN
    && !target.username
    && !target.password
    && target.search === ""
    && target.hash === ""
    && /^\/approvals\/[^/]+\/step-up$/.test(target.pathname);
}


/** 精确识别浏览器为 step-up POST 发出的 CORS 预检；不保存 Origin 或请求头原文。 */
function classifyStepUpPreflight(event) {
  const method = String(event?.request?.method ?? "GET").toUpperCase();
  if (method !== "OPTIONS") return "not_preflight";
  if (classifyConsoleUrl(String(event?.request?.url ?? "")) !== "approval_step_up") {
    return "not_preflight";
  }
  // 已被归为 step-up 的预检仍须通过完整 URL 校验，宽松分类不能替代安全路由证明。
  if (!exactStepUpRoute(event?.request?.url)) return "invalid";
  const headers = event?.request?.headers ?? {};
  const origin = readHeader(headers, "Origin");
  const requestedMethod = readHeader(headers, "Access-Control-Request-Method").toUpperCase();
  const requestedHeaders = readHeader(headers, "Access-Control-Request-Headers")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .sort();
  return CONSOLE_ORIGINS.has(origin)
    && requestedMethod === "POST"
    && JSON.stringify(requestedHeaders) === JSON.stringify(STEP_UP_PREFLIGHT_HEADERS)
    ? "valid"
    : "invalid";
}


/** request-stage 完整验证预检头；response-stage 仅凭同一 networkId 对账，不依赖重复 Header。 */
function registerStepUpPreflightRequest(tracker, event) {
  const classification = classifyStepUpPreflight(event);
  if (classification === "not_preflight") return "ignored";
  const requestId = String(event?.requestId ?? "");
  const networkId = String(event?.networkId ?? "");
  const owner = Object.freeze({
    category: "approval_step_up",
    method: "OPTIONS",
    phase: String(tracker.phase || "unknown"),
    fetchResponseStatus: null,
    networkResponseStatus: null,
  });
  if (classification !== "valid") {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_preflight");
    return "invalid";
  }
  if (!requestId || !networkId) {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_preflight_network_id");
    return "invalid";
  }
  tracker.stepUpPreflightOwners ??= new Map();
  if (tracker.stepUpPreflightOwners.has(networkId)) {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_preflight_duplicate");
    return "invalid";
  }
  const networkOwner = tracker.requestOwners.get(networkId);
  if (networkOwner && !stepUpPreflightOwnersMatch(networkOwner, owner)) {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_preflight_owner_mismatch");
    return "invalid";
  }
  tracker.stepUpPreflightOwners.set(networkId, owner);
  recordNetworkActivity(tracker);
  return owner;
}


/** 将 Fetch 预检响应绑定同一 Network id，并要求唯一 200 与 Network 状态一致。 */
function registerStepUpPreflightResponse(tracker, event, status, stage) {
  const requestId = String(event?.requestId ?? "");
  const networkId = String(event?.networkId ?? "");
  requireCondition(requestId && networkId, stage, "preflight_network_id");
  requireCondition(
    String(event?.request?.method ?? "").toUpperCase() === "OPTIONS"
      && exactStepUpRoute(event?.request?.url),
    stage,
    "preflight_route",
  );
  const owner = tracker.stepUpPreflightOwners?.get(networkId);
  requireCondition(owner, stage, "preflight_owner");
  const networkOwner = tracker.requestOwners.get(networkId);
  requireCondition(
    !networkOwner || stepUpPreflightOwnersMatch(networkOwner, owner),
    stage,
    "preflight_owner_mismatch",
  );
  requireCondition(owner.fetchResponseStatus === null, stage, "preflight_duplicate_response");
  requireCondition(status === 200, stage, "preflight_status");
  if (owner.networkResponseStatus !== null) {
    requireCondition(owner.networkResponseStatus === status, stage, "preflight_status_mismatch");
  }
  tracker.stepUpPreflightOwners.set(
    networkId,
    Object.freeze({ ...owner, fetchResponseStatus: status }),
  );
  recordNetworkActivity(tracker);
  return Object.freeze({ requestId, networkId, status });
}


/**
 * Fetch request-stage 可先于或后于 Network.requestWillBeSent。只按同一 networkId 保存精确的
 * step-up POST 身份；动态 URL 和审批 ID 不进入账本或失败坐标。
 */
function registerFetchStepUpOwner(tracker, event) {
  const url = String(event?.request?.url ?? "");
  if (classifyConsoleUrl(url) !== "approval_step_up") return "ignored";
  const requestId = String(event?.requestId ?? "");
  const networkId = String(event?.networkId ?? "");
  const method = String(event?.request?.method ?? "GET").toUpperCase();
  const owner = Object.freeze({
    category: "approval_step_up",
    method,
    phase: String(tracker.phase || "unknown"),
  });
  // Fetch owner 必须再次精确验证 Control step-up URL，动态路径或凭据漂移立即失败关闭。
  if (!exactStepUpRoute(url)) {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_route");
    return "invalid";
  }
  if (!requestId || !networkId) {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_network_id");
    return "invalid";
  }
  if (method !== "POST") {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_method");
    return "invalid";
  }
  tracker.fetchStepUpOwners ??= new Map();
  if (tracker.fetchStepUpOwners.has(networkId)) {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_duplicate_hint");
    return "invalid";
  }
  const networkOwner = tracker.requestOwners.get(networkId);
  if (networkOwner && !stepUpOwnersMatch(networkOwner, owner)) {
    recordUnexpectedNetworkFailure(tracker, owner, "step_up_owner_mismatch");
    return "invalid";
  }
  tracker.fetchStepUpOwners.set(networkId, owner);
  recordNetworkActivity(tracker);
  return owner;
}


/** 若网络账本存在异常则立即抛出首个固定坐标，使 DOM 等待不能掩盖网络故障。 */
function throwUnexpectedNetworkFailure(tracker) {
  if (Number(tracker.unexpectedNetworkFailureCount ?? 0) === 0) return false;
  const failure = tracker.unexpectedNetworkFailure ?? {
    phase: "unknown",
    category: "unowned",
    reason: "other",
  };
  throw new BrowserAcceptanceFailure(
    `browser.network.${failure.phase}.${failure.category}`,
    `loading_${failure.reason}`,
  );
}


/**
 * 为一次健康探测登记固定 logical owner。账本仅驻留内存，不进入最终证据；同一 logical ID
 * 只能有一个 attempt 1 和一个 attempt 2，且两个 attempt 必须命中相同健康路由。
 */
function registerHealthRequest(tracker, owner, requestId) {
  const healthAttempt = owner?.healthAttempt;
  if (!healthAttempt) {
    recordUnexpectedNetworkFailure(tracker, owner, "health_attempt_id");
    return false;
  }
  const key = healthAttempt.logicalId;
  let ledger = tracker.healthRequestLedgers.get(key);
  if (!ledger) {
    ledger = {
      category: owner.category,
      state: "pending",
      failureCounted: false,
      recoveryCounted: false,
      attempts: { 1: null, 2: null },
    };
    tracker.healthRequestLedgers.set(key, ledger);
  }
  if (ledger.category !== owner.category) {
    ledger.state = "invalid";
    recordUnexpectedNetworkFailure(tracker, owner, "health_route");
    return false;
  }
  if (ledger.attempts[healthAttempt.attempt]) {
    ledger.state = "invalid";
    recordUnexpectedNetworkFailure(tracker, owner, "health_duplicate_attempt");
    return false;
  }
  tracker.healthRequestIds.set(requestId, Object.freeze({
    logicalId: key,
    attempt: healthAttempt.attempt,
  }));
  ledger.attempts[healthAttempt.attempt] = {
    requestId,
    phase: owner.phase,
    responseStatus: null,
    terminal: null,
    failureReason: null,
  };
  if (healthAttempt.attempt === 2 && ledger.state === "succeeded") {
    ledger.state = "invalid";
    recordUnexpectedNetworkFailure(tracker, owner, "health_redundant_retry");
    return false;
  }
  return true;
}


/**
 * Chromium 可能把自定义请求头延后放进 requestWillBeSentExtraInfo；Fetch request-stage 则持有
 * 同一 networkId 的实际请求头。两条事件任一先到都只能为 provisional owner 补一次身份。
 */
function attachHealthRequestHeaders(tracker, requestId, headers) {
  const healthAttempt = parseHealthAttemptRequestId(readHeader(headers, "X-Request-ID"));
  const owner = tracker.requestOwners.get(requestId);
  if (!owner) {
    if (!healthAttempt) return "missing";
    tracker.healthRequestHeaderHints.set(requestId, healthAttempt);
    return "deferred";
  }
  if (owner.method !== "GET" || !HEALTH_REQUEST_CATEGORIES.has(owner.category)) {
    tracker.healthRequestHeaderHints.delete(requestId);
    return "not_health";
  }
  if (!healthAttempt) return "missing";
  if (owner.healthAttempt) {
    if (
      owner.healthAttempt.logicalId !== healthAttempt.logicalId
      || owner.healthAttempt.attempt !== healthAttempt.attempt
    ) {
      const ledger = tracker.healthRequestLedgers.get(owner.healthAttempt.logicalId);
      if (ledger) ledger.state = "invalid";
      recordUnexpectedNetworkFailure(tracker, owner, "health_attempt_mismatch");
      return "mismatch";
    }
    return "already_attached";
  }
  const attached = Object.freeze({ ...owner, healthAttempt });
  tracker.requestOwners.set(requestId, attached);
  return registerHealthRequest(tracker, attached, requestId) ? "attached" : "invalid";
}


/** 登记 CDP request owner，并拒绝活跃 ID 覆盖、健康重定向和非法 attempt 标识。 */
function recordNetworkRequest(tracker, event) {
  const requestId = String(event?.requestId ?? "");
  const request = event?.request;
  if (!requestId || !request?.url) return null;
  recordNetworkActivity(tracker);
  const method = String(request.method ?? "GET").toUpperCase();
  const category = classifyRequest(request.url, method);
  const hintedHealthAttempt = tracker.healthRequestHeaderHints.get(requestId) ?? null;
  tracker.healthRequestHeaderHints.delete(requestId);
  const healthAttempt = method === "GET" && HEALTH_REQUEST_CATEGORIES.has(category)
    ? parseHealthAttemptRequestId(readHeader(request.headers, "X-Request-ID"))
      ?? hintedHealthAttempt
    : null;
  const owner = Object.freeze({
    category,
    method,
    phase: String(tracker.phase || "unknown"),
    healthAttempt,
    ...(method === "OPTIONS" && HEALTH_REQUEST_CATEGORIES.has(category)
      ? healthPreflightIdentity(request.url, request.headers)
      : {}),
  });
  registerLoginNetworkRequest(tracker, event, owner);
  const fetchStepUpOwner = tracker.fetchStepUpOwners?.get(requestId);
  if (fetchStepUpOwner && !stepUpOwnersMatch(fetchStepUpOwner, owner)) {
    recordUnexpectedNetworkFailure(tracker, fetchStepUpOwner, "step_up_owner_mismatch");
  }
  if (fetchStepUpOwner && event?.redirectResponse) {
    recordUnexpectedNetworkFailure(tracker, fetchStepUpOwner, "step_up_redirect");
  }
  const preflightOwner = tracker.stepUpPreflightOwners?.get(requestId);
  if (preflightOwner && !stepUpPreflightOwnersMatch(preflightOwner, owner)) {
    recordUnexpectedNetworkFailure(tracker, preflightOwner, "step_up_preflight_owner_mismatch");
  }
  if (preflightOwner && event?.redirectResponse) {
    recordUnexpectedNetworkFailure(tracker, preflightOwner, "step_up_preflight_redirect");
  }
  const healthPreflightOwner = tracker.healthPreflightOwners?.get(requestId);
  if (healthPreflightOwner && !healthPreflightOwnersMatch(healthPreflightOwner, owner)) {
    recordUnexpectedNetworkFailure(tracker, healthPreflightOwner, "health_preflight_owner_mismatch");
  }
  if (healthPreflightOwner && event?.redirectResponse) {
    recordUnexpectedNetworkFailure(tracker, healthPreflightOwner, "health_preflight_redirect");
  }
  const previous = tracker.requestOwners.get(requestId);
  if (previous && !event?.redirectResponse) {
    recordUnexpectedNetworkFailure(tracker, previous, "request_id_reuse");
    return previous;
  }
  if (event?.redirectResponse && (
    HEALTH_REQUEST_CATEGORIES.has(previous?.category)
    || HEALTH_REQUEST_CATEGORIES.has(category)
  )) {
    const ledger = previous?.healthAttempt
      ? tracker.healthRequestLedgers.get(previous.healthAttempt.logicalId)
      : null;
    if (ledger) ledger.state = "invalid";
    recordUnexpectedNetworkFailure(tracker, previous ?? owner, "health_redirect");
  }
  tracker.requestOwners.set(requestId, owner);
  if (method === "GET" && HEALTH_REQUEST_CATEGORIES.has(category) && healthAttempt) {
    registerHealthRequest(tracker, owner, requestId);
  }
  return owner;
}


/** 返回 owner 对应的唯一 attempt 状态；任何漂移都转为固定失败坐标。 */
function healthAttemptState(tracker, owner, requestId) {
  const healthAttempt = owner?.healthAttempt;
  if (!healthAttempt) {
    recordUnexpectedNetworkFailure(tracker, owner, "health_attempt_id");
    return null;
  }
  const ledger = healthAttempt
    ? tracker.healthRequestLedgers.get(healthAttempt.logicalId)
    : null;
  const attempt = ledger?.attempts?.[healthAttempt?.attempt];
  if (
    !ledger
    || ledger.category !== owner?.category
    || !attempt
    || attempt.requestId !== requestId
  ) {
    if (ledger) ledger.state = "invalid";
    recordUnexpectedNetworkFailure(tracker, owner, "health_owner");
    return null;
  }
  return { ledger, attempt };
}


/**
 * 统一收敛 attempt 事件反序。只有“attempt 1 明确网络失败 + attempt 2 同路由 200 且
 * loadingFinished”能够形成 recovered；attempt 1 成功后出现 retry 必须失败。
 */
function reconcileHealthLedger(tracker, ledger) {
  if (!ledger || ledger.state === "invalid") return "invalid";
  const first = ledger.attempts[1];
  const retry = ledger.attempts[2];
  const owner = Object.freeze({
    category: ledger.category,
    phase: String(retry?.phase ?? first?.phase ?? "unknown"),
  });
  if (retry?.terminal === "failed") {
    ledger.state = "invalid";
    recordUnexpectedNetworkFailure(tracker, owner, "health_retry_failed");
    return ledger.state;
  }
  if (first?.terminal === "finished" && first.responseStatus !== null) {
    if (first.responseStatus !== 200) {
      ledger.state = "invalid";
      recordUnexpectedNetworkFailure(tracker, owner, "health_status");
    } else if (retry) {
      ledger.state = "invalid";
      recordUnexpectedNetworkFailure(tracker, owner, "health_redundant_retry");
    } else {
      ledger.state = "succeeded";
    }
    return ledger.state;
  }
  if (
    first?.terminal === "failed"
    && retry?.terminal === "finished"
    && retry.responseStatus !== null
  ) {
    if (retry.responseStatus !== 200) {
      ledger.state = "invalid";
      recordUnexpectedNetworkFailure(tracker, owner, "health_status");
    } else {
      ledger.state = "recovered";
      if (!ledger.recoveryCounted) {
        ledger.recoveryCounted = true;
        tracker.healthProbeRecoveryCount = Number(tracker.healthProbeRecoveryCount ?? 0) + 1;
      }
    }
  }
  return ledger.state;
}


/** 已知健康 request id 出现失去 owner 的事件时使整条 logical ledger 失败关闭。 */
function recordUnownedHealthEvent(tracker, requestId, reason) {
  const known = tracker.healthRequestIds.get(requestId);
  if (!known) return false;
  const ledger = tracker.healthRequestLedgers.get(known.logicalId);
  if (ledger) ledger.state = "invalid";
  recordUnexpectedNetworkFailure(tracker, {
    category: ledger?.category,
    phase: String(tracker.phase || "unknown"),
  }, reason);
  return true;
}


/** 精确归因 Network loadingFailed；只有登记的 step-up 取消或健康首试失败可被账本接纳。 */
function recordNetworkLoadingFailure(tracker, event) {
  recordNetworkActivity(tracker);
  const requestId = String(event?.requestId ?? "");
  const networkOwner = tracker.requestOwners.get(requestId);
  const fetchStepUpOwner = tracker.fetchStepUpOwners?.get(requestId);
  const preflightOwner = tracker.stepUpPreflightOwners?.get(requestId);
  const healthPreflightOwner = tracker.healthPreflightOwners?.get(requestId);
  if (tracker.loginRequestProofs?.has(requestId)) {
    tracker.requestOwners.delete(requestId);
    tracker.expectedCanceledRequestIds.delete(requestId);
    recordLoginNetworkTerminal(tracker, requestId, "failed");
    return "unexpected_login_failure";
  }
  if (healthPreflightOwner || (
    HEALTH_REQUEST_CATEGORIES.has(networkOwner?.category) && networkOwner.method === "OPTIONS"
  )) {
    tracker.requestOwners.delete(requestId);
    tracker.healthPreflightOwners?.delete(requestId);
    tracker.expectedCanceledRequestIds.delete(requestId);
    recordUnexpectedNetworkFailure(
      tracker,
      healthPreflightOwner ?? networkOwner,
      healthPreflightOwner
        && networkOwner
        && healthPreflightOwnersMatch(networkOwner, healthPreflightOwner)
        ? "health_preflight_failed"
        : "health_preflight_owner_mismatch",
    );
    return "unexpected_health_preflight_failure";
  }
  if (preflightOwner || (
    networkOwner?.category === "approval_step_up" && networkOwner.method === "OPTIONS"
  )) {
    tracker.requestOwners.delete(requestId);
    tracker.stepUpPreflightOwners?.delete(requestId);
    tracker.expectedCanceledRequestIds.delete(requestId);
    recordUnexpectedNetworkFailure(
      tracker,
      preflightOwner ?? networkOwner,
      preflightOwner && (!networkOwner || stepUpPreflightOwnersMatch(networkOwner, preflightOwner))
        ? "step_up_preflight_failed"
        : "step_up_preflight_owner_mismatch",
    );
    return "unexpected_preflight_failure";
  }
  const ownersMatch = !networkOwner || !fetchStepUpOwner
    || stepUpOwnersMatch(networkOwner, fetchStepUpOwner);
  if (!ownersMatch) {
    recordUnexpectedNetworkFailure(
      tracker,
      fetchStepUpOwner ?? networkOwner,
      "step_up_owner_mismatch",
    );
  }
  const owner = networkOwner ?? fetchStepUpOwner;
  tracker.requestOwners.delete(requestId);
  tracker.fetchStepUpOwners?.delete(requestId);
  const expectedCancellation = tracker.expectedCanceledRequestIds.delete(requestId);
  const reason = classifyNetworkFailure(event?.errorText);
  if (!owner && recordUnownedHealthEvent(tracker, requestId, "health_unowned_terminal")) {
    return "unexpected_health_terminal";
  }
  if (
    expectedCancellation
    && ownersMatch
    && owner?.category === "approval_step_up"
    && owner.method === "POST"
    && ["aborted", "failed"].includes(reason)
  ) {
    tracker.canceledStepUpRequestCount = Number(tracker.canceledStepUpRequestCount ?? 0) + 1;
    return "expected_step_up_cancel";
  }
  if (owner?.method === "GET" && HEALTH_REQUEST_CATEGORIES.has(owner.category)) {
    const state = healthAttemptState(tracker, owner, requestId);
    if (!state) return "unexpected_failure";
    if (
      owner.healthAttempt.attempt === 1
      && ["connection_refused", "failed"].includes(reason)
    ) {
      if (state.attempt.terminal !== null) {
        state.ledger.state = "invalid";
        recordUnexpectedNetworkFailure(tracker, owner, "health_duplicate_terminal");
        return "unexpected_health_terminal";
      }
      state.attempt.terminal = "failed";
      state.attempt.failureReason = reason;
      if (!state.ledger.failureCounted) {
        state.ledger.failureCounted = true;
        tracker.healthProbeFailureCount = Number(tracker.healthProbeFailureCount ?? 0) + 1;
      }
      reconcileHealthLedger(tracker, state.ledger);
      return "recoverable_health_failure";
    }
    state.ledger.state = "invalid";
  }
  recordUnexpectedNetworkFailure(tracker, owner, reason);
  return "unexpected_failure";
}


/** 已登记为预期取消的请求如果正常完成，必须作为验收失败处理。 */
function recordNetworkLoadingFinished(tracker, event) {
  recordNetworkActivity(tracker);
  const requestId = String(event?.requestId ?? "");
  const networkOwner = tracker.requestOwners.get(requestId);
  const fetchStepUpOwner = tracker.fetchStepUpOwners?.get(requestId);
  const preflightOwner = tracker.stepUpPreflightOwners?.get(requestId);
  const healthPreflightOwner = tracker.healthPreflightOwners?.get(requestId);
  if (tracker.loginRequestProofs?.has(requestId)) {
    tracker.requestOwners.delete(requestId);
    tracker.expectedCanceledRequestIds.delete(requestId);
    return recordLoginNetworkTerminal(tracker, requestId, "finished");
  }
  if (healthPreflightOwner || (
    HEALTH_REQUEST_CATEGORIES.has(networkOwner?.category) && networkOwner.method === "OPTIONS"
  )) {
    tracker.requestOwners.delete(requestId);
    tracker.healthPreflightOwners?.delete(requestId);
    const expectedCancellation = tracker.expectedCanceledRequestIds.delete(requestId);
    const valid = healthPreflightOwner
      && networkOwner
      && healthPreflightOwnersMatch(networkOwner, healthPreflightOwner)
      && healthPreflightOwner.networkResponseStatus === 200
      && healthPreflightOwner.corsResponseAllowed === true
      && !expectedCancellation;
    if (!valid) {
      recordUnexpectedNetworkFailure(
        tracker,
        healthPreflightOwner ?? networkOwner,
        "health_preflight_terminal",
      );
      return "unexpected_health_preflight_finish";
    }
    return "health_preflight_finished";
  }
  if (preflightOwner || (
    networkOwner?.category === "approval_step_up" && networkOwner.method === "OPTIONS"
  )) {
    tracker.requestOwners.delete(requestId);
    tracker.stepUpPreflightOwners?.delete(requestId);
    const expectedCancellation = tracker.expectedCanceledRequestIds.delete(requestId);
    const valid = preflightOwner
      && networkOwner
      && stepUpPreflightOwnersMatch(networkOwner, preflightOwner)
      && preflightOwner.fetchResponseStatus === 200
      && preflightOwner.networkResponseStatus === 200
      && !expectedCancellation;
    if (!valid) {
      recordUnexpectedNetworkFailure(
        tracker,
        preflightOwner ?? networkOwner,
        "step_up_preflight_terminal",
      );
      return "unexpected_preflight_finish";
    }
    return "step_up_preflight_finished";
  }
  const ownersMatch = !networkOwner || !fetchStepUpOwner
    || stepUpOwnersMatch(networkOwner, fetchStepUpOwner);
  if (!ownersMatch) {
    recordUnexpectedNetworkFailure(
      tracker,
      fetchStepUpOwner ?? networkOwner,
      "step_up_owner_mismatch",
    );
  }
  const owner = networkOwner ?? fetchStepUpOwner;
  tracker.fetchStepUpOwners?.delete(requestId);
  if (!owner && recordUnownedHealthEvent(tracker, requestId, "health_unowned_terminal")) {
    return "unexpected_health_terminal";
  }
  if (tracker.expectedCanceledRequestIds.delete(requestId)) {
    tracker.requestOwners.delete(requestId);
    recordUnexpectedNetworkFailure(tracker, owner, "finished");
    return "unexpected_finish";
  }
  if (HEALTH_REQUEST_CATEGORIES.has(owner?.category) && owner?.method === "GET") {
    const state = healthAttemptState(tracker, owner, requestId);
    if (!state) {
      tracker.requestOwners.delete(requestId);
      return "unexpected_health_attempt";
    }
    if (state.attempt.terminal !== null) {
      state.ledger.state = "invalid";
      recordUnexpectedNetworkFailure(tracker, owner, "health_duplicate_terminal");
      tracker.requestOwners.delete(requestId);
      return "unexpected_health_terminal";
    }
    state.attempt.terminal = "finished";
    const responseStatus = owner.responseStatus ?? null;
    state.attempt.responseStatus = responseStatus;
    if (responseStatus === null) {
      tracker.requestOwners.set(requestId, Object.freeze({ ...owner, terminal: "finished" }));
      return "health_finished_waiting_status";
    }
    tracker.requestOwners.delete(requestId);
    reconcileHealthLedger(tracker, state.ledger);
    return owner.healthAttempt.attempt === 2 ? "health_retry_finished" : "health_finished";
  }
  tracker.requestOwners.delete(requestId);
  return "finished";
}


/** 响应先只记状态；健康恢复必须等 loadingFinished 证明正文传输完成。 */
function recordNetworkResponse(tracker, event) {
  recordNetworkActivity(tracker);
  const requestId = String(event?.requestId ?? "");
  const loginProof = tracker.loginRequestProofs?.get(requestId);
  if (loginProof) {
    const status = Number(event?.response?.status ?? 0);
    recordResponse(tracker, requestId, status, loginProof.category);
    return recordLoginNetworkResponse(tracker, requestId, status);
  }
  const owner = tracker.requestOwners.get(requestId);
  if (!owner) {
    if (recordUnownedHealthEvent(tracker, requestId, "health_unowned_response")) {
      return "unexpected_health_response";
    }
    return "unowned";
  }
  const status = Number(event?.response?.status ?? 0);
  if (HEALTH_REQUEST_CATEGORIES.has(owner.category) && owner.method === "OPTIONS") {
    const preflightOwner = tracker.healthPreflightOwners?.get(requestId);
    if (
      !preflightOwner
      || !healthPreflightOwnersMatch(owner, preflightOwner)
      || preflightOwner.networkResponseStatus !== null
      || status !== 200
      || !healthPreflightResponseAllowed(preflightOwner, event?.response?.headers)
    ) {
      recordUnexpectedNetworkFailure(
        tracker,
        preflightOwner ?? owner,
        "health_preflight_response",
      );
      return "unexpected_health_preflight_response";
    }
    tracker.healthPreflightOwners.set(
      requestId,
      Object.freeze({
        ...preflightOwner,
        networkResponseStatus: status,
        corsResponseAllowed: true,
      }),
    );
    tracker.requestOwners.set(requestId, Object.freeze({ ...owner, responseStatus: status }));
    return "health_preflight_response";
  }
  if (owner.category === "approval_step_up" && owner.method === "OPTIONS") {
    const preflightOwner = tracker.stepUpPreflightOwners?.get(requestId);
    if (
      !preflightOwner
      || !stepUpPreflightOwnersMatch(owner, preflightOwner)
      || preflightOwner.networkResponseStatus !== null
      || status !== 200
      || (preflightOwner.fetchResponseStatus !== null && preflightOwner.fetchResponseStatus !== status)
    ) {
      recordUnexpectedNetworkFailure(
        tracker,
        preflightOwner ?? owner,
        "step_up_preflight_response",
      );
      return "unexpected_preflight_response";
    }
    tracker.stepUpPreflightOwners.set(
      requestId,
      Object.freeze({ ...preflightOwner, networkResponseStatus: status }),
    );
    tracker.requestOwners.set(requestId, Object.freeze({ ...owner, responseStatus: status }));
    return "step_up_preflight_response";
  }
  if (HEALTH_REQUEST_CATEGORIES.has(owner.category) && owner.method === "GET") {
    const state = healthAttemptState(tracker, owner, requestId);
    if (!state) return "unexpected_health_response";
    if (state.attempt.responseStatus !== null) {
      state.ledger.state = "invalid";
      recordUnexpectedNetworkFailure(tracker, owner, "health_duplicate_response");
      return "unexpected_health_response";
    }
    state.attempt.responseStatus = status;
    if (owner.terminal === "finished") {
      tracker.requestOwners.delete(requestId);
      reconcileHealthLedger(tracker, state.ledger);
      return "health_response_finished";
    }
  }
  tracker.requestOwners.set(requestId, Object.freeze({ ...owner, responseStatus: status }));
  if (owner.method !== "OPTIONS") recordResponse(tracker, requestId, status, owner.category);
  return "recorded";
}


/** Fetch 控制 id 与 Network 归因 id 分开保存；缺失或错 owner 时立即失败关闭。 */
function registerPausedStepUpResponse(tracker, event, status, stage) {
  const requestId = String(event?.requestId ?? "");
  const networkId = String(event?.networkId ?? "");
  requireCondition(requestId && networkId, stage, "network_id");
  const method = String(event?.request?.method ?? "GET").toUpperCase();
  requireCondition(
    exactStepUpRoute(String(event?.request?.url ?? "")),
    stage,
    "network_route",
  );
  requireCondition(method === "POST", stage, "network_method");
  const networkOwner = tracker.requestOwners.get(networkId);
  const fetchStepUpOwner = tracker.fetchStepUpOwners?.get(networkId);
  requireCondition(
    !networkOwner || !fetchStepUpOwner || stepUpOwnersMatch(networkOwner, fetchStepUpOwner),
    stage,
    "network_owner_mismatch",
  );
  const owner = networkOwner ?? fetchStepUpOwner;
  requireCondition(
    owner?.category === "approval_step_up" && owner.method === "POST",
    stage,
    "network_owner",
  );
  requireCondition(!tracker.expectedCanceledRequestIds.has(networkId), stage, "network_duplicate");
  tracker.expectedCanceledRequestIds.add(networkId);
  return Object.freeze({ requestId, networkId, status });
}


/** Console 的无 request id ERR_FAILED/ERR_ABORTED 只记候选，最终受精确 Network 失败总数约束。 */
function recordBrowserConsoleEntry(
  tracker,
  { type = "", text = "", url = "", argumentCount = -1, source = "log" } = {},
) {
  const value = String(text ?? "");
  // 子进程或页面 Console 一旦包含本轮私密值即计为泄露，原始文本仍不得进入证据。
  if ((tracker.privateValues ?? []).some((secret) => secret && value.includes(secret))) {
    tracker.processPrivacyViolations = Number(tracker.processPrivacyViolations ?? 0) + 1;
  }
  const classification = classifyBrowserConsoleError({ type, text: value, url, argumentCount });
  if (classification === "ignored") return "ignored";
  if (classification === "expected_step_up_403") {
    tracker.expectedStepUpConsoleErrors = Number(tracker.expectedStepUpConsoleErrors ?? 0) + 1;
    return "expected_step_up_403";
  }
  const reason = classifyUnexpectedConsoleReason({ type, text: value, source });
  if (
    source === "log"
    && argumentCount === 0
    && ["net_aborted", "net_failed", "net_refused"].includes(reason)
  ) {
    // Chromium Log 不提供 Network request id 且可能省略取消日志；这里只登记候选，不能替代 Network 证明。
    tracker.consoleNetworkFailureCount = Number(tracker.consoleNetworkFailureCount ?? 0) + 1;
    return "network_failure_candidate";
  }
  tracker.unexpectedConsoleErrors = Number(tracker.unexpectedConsoleErrors ?? 0) + 1;
  tracker.unexpectedConsoleReasons ??= new Map();
  const coordinate = [
    String(tracker.phase || "unknown"),
    source === "runtime" ? "runtime" : "log",
    classifyConsoleUrl(url),
    reason,
  ].join("_");
  tracker.unexpectedConsoleReasons.set(
    coordinate,
    (tracker.unexpectedConsoleReasons.get(coordinate) ?? 0) + 1,
  );
  return "unexpected_error";
}


/**
 * 安装请求防火墙、响应状态账本与 Console/exception 门禁。
 * Fetch 在 step-up 响应阶段额外暂停一次，用真实 200/403 验证 owner 漂移后的迟到结果丢弃。
 */
async function bindBrowserGuards(client, tracker) {
  client.on("Network.requestWillBeSent", (event) => {
    const { requestId, request } = event;
    if (!requestId || !request?.url) return;
    const owner = recordNetworkRequest(tracker, event);
    const category = owner?.category ?? classifyRequest(request.url, request.method);
    let target;
    try { target = new URL(request.url); } catch {
      tracker.externalRequests += 1;
      return;
    }
    if (category === "external") tracker.externalRequests += 1;
    if ([...target.searchParams.keys()].some((key) => FORBIDDEN_QUERY_KEY.test(key))) {
      tracker.forbiddenQueries += 1;
    }
    const authorization = readHeader(request.headers, "authorization");
    const bearer = authorization.match(/^Bearer ([^\s]+)$/)?.[1] ?? "";
    // Bearer 只在内存集合中用于后续泄露检测，不写入最终 JSON、截图或错误坐标。
    if (bearer) tracker.browserTokens.add(bearer);
    // Authorization 只能发往固定 Control Origin，跨服务携带即视为隐私边界违规。
    if (authorization && target.origin !== CONTROL_ORIGIN) tracker.requestPrivacyViolations += 1;
    const body = String(request.postData ?? "");
    const allowedPrivateBody = category === "auth_login" || category === "approval_step_up";
    if (!allowedPrivateBody && tracker.privateValues.some((value) => value && body.includes(value))) {
      tracker.requestPrivacyViolations += 1;
    }
    for (const token of tracker.browserTokens) {
      // Session Token 不得出现在 URL 或正文；命中时只增加计数，不保留敏感值本身。
      if (token && (request.url.includes(token) || body.includes(token))) {
        tracker.requestPrivacyViolations += 1;
      }
    }
  });

  client.on("Network.requestWillBeSentExtraInfo", ({ requestId, headers }) => {
    if (!requestId) return;
    attachHealthRequestHeaders(tracker, String(requestId), headers);
  });

  client.on("Network.responseReceived", (event) => { recordNetworkResponse(tracker, event); });
  client.on("Network.loadingFinished", (event) => { recordNetworkLoadingFinished(tracker, event); });
  client.on("Network.loadingFailed", (event) => { recordNetworkLoadingFailure(tracker, event); });

  /** 把 Runtime 与 Log 事件统一送入脱敏分类器，回调本身不保存 Console 正文。 */
  const inspectConsole = (type, text, url = "", argumentCount = -1, source = "log") => {
    recordBrowserConsoleEntry(tracker, { type, text, url, argumentCount, source });
  };
  client.on("Runtime.consoleAPICalled", ({ type, args }) => {
    const values = args ?? [];
    inspectConsole(
      type,
      values.map((item) => item.value ?? item.description ?? "").join(" "),
      "",
      values.length,
      "runtime",
    );
  });
  client.on("Log.entryAdded", ({ entry }) => inspectConsole(
    entry?.level,
    entry?.text,
    entry?.url,
    0,
    "log",
  ));
  client.on("Runtime.exceptionThrown", () => { tracker.runtimeExceptions += 1; });

  client.on("Fetch.requestPaused", (event) => {
    const requestId = String(event.requestId ?? "");
    const networkId = String(event.networkId ?? "");
    const url = String(event.request?.url ?? "");
    const method = String(event.request?.method ?? "GET");
    if (networkId) attachHealthRequestHeaders(tracker, networkId, event.request?.headers);
    const responseStage = Number.isFinite(Number(event.responseStatusCode));
    const stepUpPreflight = classifyStepUpPreflight(event);
    const healthPreflight = classifyHealthPreflight(event);
    if (responseStage) {
      if (method.toUpperCase() === "OPTIONS") {
        try {
          registerStepUpPreflightResponse(
            tracker,
            event,
            Number(event.responseStatusCode),
            `browser.network.${String(tracker.phase || "unknown")}.preflight`,
          );
        } catch (error) {
          const failure = safeFailure(error, "browser.network.preflight");
          recordUnexpectedNetworkFailure(tracker, {
            category: "approval_step_up",
            method: "OPTIONS",
            phase: String(tracker.phase || "unknown"),
          }, failure.code);
        }
        void client.send("Fetch.continueResponse", { requestId }).catch(() => {
          tracker.browserProcessErrors += 1;
        });
        return;
      }
      if (networkId && tracker.expectedCanceledRequestIds.has(networkId)) {
        recordUnexpectedNetworkFailure(
          tracker,
          tracker.requestOwners.get(networkId) ?? tracker.fetchStepUpOwners.get(networkId),
          "step_up_duplicate_response",
        );
        void client.send("Fetch.continueResponse", { requestId }).catch(() => {
          tracker.browserProcessErrors += 1;
        });
        return;
      }
      const expectation = tracker.pauseExpectation;
      if (expectation && tracker.pausedResponse === null) {
        const status = Number(event.responseStatusCode);
        if (status !== expectation.status) {
          expectation.reject(new BrowserAcceptanceFailure(expectation.stage, "status"));
          tracker.pauseExpectation = null;
          void client.send("Fetch.continueResponse", { requestId }).catch(() => {});
          return;
        }
        try {
          tracker.pausedResponse = registerPausedStepUpResponse(
            tracker,
            event,
            status,
            expectation.stage,
          );
        } catch (error) {
          expectation.reject(error);
          tracker.pauseExpectation = null;
          void client.send("Fetch.continueResponse", { requestId }).catch(() => {});
          return;
        }
        tracker.interceptedStepUpStatuses.push(status);
        tracker.pauseExpectation = null;
        expectation.resolve(tracker.pausedResponse);
        return;
      }
      void client.send("Fetch.continueResponse", { requestId }).catch(() => {
        tracker.browserProcessErrors += 1;
      });
      return;
    }
    if (stepUpPreflight !== "not_preflight") {
      registerStepUpPreflightRequest(tracker, event);
      void client.send("Fetch.continueRequest", { requestId }).catch(() => {
        tracker.browserProcessErrors += 1;
      });
      return;
    }
    if (healthPreflight !== "not_preflight") {
      registerHealthPreflightRequest(tracker, event);
      void client.send("Fetch.continueRequest", { requestId }).catch(() => {
        tracker.browserProcessErrors += 1;
      });
      return;
    }
    registerFetchStepUpOwner(tracker, event);
    if (!browserRequestAllowed(url)) {
      if (classifyRequest(url, method) === "external") tracker.externalRequests += 1;
      void client.send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }).catch(() => {
        tracker.browserProcessErrors += 1;
      });
      return;
    }
    void client.send("Fetch.continueRequest", { requestId }).catch(() => {
      tracker.browserProcessErrors += 1;
    });
  });

  await client.send("Fetch.disable");
  await client.send("Fetch.enable", {
    patterns: [
      { urlPattern: "*", requestStage: "Request" },
      { urlPattern: `${CONTROL_ORIGIN}/approvals/*/step-up`, requestStage: "Response" },
    ],
  });
}


/** 登记下一条精确 step-up 响应暂停预期，用于验证离页后迟到结果必须被丢弃。 */
function pauseNextStepUp(tracker, status, stage) {
  requireCondition(!tracker.pauseExpectation && !tracker.pausedResponse, stage, "already_pending");
  return new Promise((resolve, reject) => {
    tracker.pauseExpectation = { status, stage, resolve, reject };
    setTimeout(() => {
      if (tracker.pauseExpectation?.resolve === resolve) {
        tracker.pauseExpectation = null;
        reject(new BrowserAcceptanceFailure(stage, "timeout"));
      }
    }, 30_000);
  });
}


/** 释放已绑定的 Fetch 响应并清空 owner；缺失或 CDP 失败均不伪造业务完成。 */
async function releasePausedResponse(client, tracker, stage) {
  const paused = tracker.pausedResponse;
  requireCondition(paused, stage, "missing");
  tracker.pausedResponse = null;
  try {
    await client.send("Fetch.continueResponse", { requestId: paused.requestId });
    return true;
  } catch {
    // 离页/注销已经 abort 的 Fetch 可能先于 continue 销毁；这正是迟到响应不能重入页面的边界。
    return false;
  }
}


async function waitForDom(client, sessionId, expression, stage) {
  // DOM 等待统一使用固定上限和安全阶段坐标，不回传页面正文、Session 或动态错误文本。
  return waitForDomBase(client, sessionId, expression, stage, DOM_WAIT_TIMEOUT_MS);
}


/**
 * 登录等待把 Network 账本作为主动失败信号；表单重现只映射为固定 code，不读取错误组件正文。
 */
async function waitForLoginCompletion(
  client,
  sessionId,
  tracker,
  attempt,
  stage,
  { timeoutMs = DOM_WAIT_TIMEOUT_MS, pollMs = 100 } = {},
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    throwUnexpectedNetworkFailure(tracker);
    const dom = await evaluate(client, sessionId, `(() => ({
      ready: Boolean(
        document.querySelector('#logout-button')
        && document.querySelector('[data-approvals-root]')
      ),
      login_form_returned: (() => {
        const form = document.querySelector('#login-form');
        return Boolean(form && form.dataset.prd078LoginOwner !== 'active');
      })(),
    }))()`, stage);
    if (dom?.ready === true) {
      assertLoginAttemptComplete(attempt, stage);
      return true;
    }
    if (dom?.login_form_returned === true) {
      throw new BrowserAcceptanceFailure(stage, "form_returned");
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
  assertLoginAttemptComplete(attempt, stage);
  throw new BrowserAcceptanceFailure(stage, "dom_timeout");
}


/** 等待连续两帧完成布局和焦点提交，只返回布尔事实而不读取页面业务正文。 */
async function settle(client, sessionId, stage) {
  // evaluate 只运行固定动画帧表达式，sessionId 仅用于绑定当前受控 CDP 页面。
  return evaluate(client, sessionId, `new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
  })`, stage);
}


/** 导航到固定 Console approvals 页面并等待登录表单，不接受外部 URL 或现有登录态。 */
async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#approvals` }, sessionId),
    "browser.console.load",
    60_000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


/** 真实表单登录；凭据只注入 password/email input 并立即由页面 POST 消费。 */
async function loginRole(client, sessionId, role, privateConfig, tracker) {
  const account = ROLE_ACCOUNTS[role];
  requireCondition(account, `browser.login.${role}`, "role");
  const attempt = beginLoginAttempt(tracker, role, `browser.login.${role}`);
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#login-form');
    if (!form) return false;
    form.dataset.prd078LoginOwner = 'active';
    history.replaceState(null, '', location.pathname + location.search + '#approvals');
    const tenant = form.elements.namedItem('tenantSlug');
    const email = form.elements.namedItem('email');
    const password = form.elements.namedItem('password');
    tenant.value = 'default';
    email.value = ${JSON.stringify(account.email)};
    password.value = ${JSON.stringify(privateConfig.rolePassword)};
    for (const field of [tenant, email, password]) {
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
    form.requestSubmit();
    return true;
  })()`, `browser.login.${role}`);
  requireCondition(submitted === true, `browser.login.${role}`, "submit");
  await waitForLoginCompletion(
    client,
    sessionId,
    tracker,
    attempt,
    `browser.login.${role}`,
  );
  await waitForRequestOwnersDrain(tracker, `browser.login.${role}.idle`);
}


/**
 * 等待已登记请求连续稳定归零。
 *
 * DOM 已完成不代表 hash 导航触发的完整 Console refresh 已结束；注销会同步销毁页面 owner 并 abort 这些
 * 读取。稳定窗口同时观察 owner 集合、健康重试账本和 activity revision，避免在 attempt 1/2 或两个
 * 顺序短请求之间误判为空闲。
 */
async function waitForRequestOwnersDrain(
  tracker,
  stage,
  {
    timeoutMs = 60_000,
    stableMs = 250,
    pollMs = 25,
    allowPausedStepUp = false,
  } = {},
) {
  const deadline = Date.now() + timeoutMs;
  let emptySince = null;
  let emptyRevision = null;
  while (Date.now() < deadline) {
    throwUnexpectedNetworkFailure(tracker);
    let ignoredNetworkId = null;
    if (allowPausedStepUp) {
      const paused = tracker.pausedResponse;
      ignoredNetworkId = String(paused?.networkId ?? "");
      const networkOwner = tracker.requestOwners?.get?.(ignoredNetworkId);
      const fetchOwner = tracker.fetchStepUpOwners?.get?.(ignoredNetworkId);
      const owner = networkOwner ?? fetchOwner;
      const expectedCanceledIds = [...(tracker.expectedCanceledRequestIds ?? [])];
      const activeOwnerValid = Boolean(
        owner
        && owner.category === "approval_step_up"
        && owner.method === "POST"
        && (!networkOwner || !fetchOwner || stepUpOwnersMatch(networkOwner, fetchOwner))
        && expectedCanceledIds.length === 1
        && expectedCanceledIds[0] === ignoredNetworkId
      );
      const terminalOwnerValid = Boolean(
        !owner
        && expectedCanceledIds.length === 0
      );
      // Chrome 可在 Fetch 控制句柄仍暂停时先结算被 AbortController 取消的 Network owner。
      // 只接受“精确 owner 仍在”或“owner 与取消登记均已完整终态清除”；半清理、错类别或
      // 额外取消请求仍失败关闭，不能用该选项掩盖普通 Console 读取仍在进行。
      requireCondition(
        Boolean(
          ignoredNetworkId
          && [200, 403].includes(Number(paused?.status))
          && (activeOwnerValid || terminalOwnerValid),
        ),
        stage,
        "paused_owner_invalid",
      );
      if (terminalOwnerValid) ignoredNetworkId = null;
    }
    const requestOwnersSettled = [...(tracker.requestOwners?.keys?.() ?? [])]
      .every((requestId) => requestId === ignoredNetworkId);
    const healthRecoveriesSettled = [...(tracker.healthRequestLedgers?.values?.() ?? [])]
      .every((ledger) => ["succeeded", "recovered"].includes(ledger.state));
    const fetchStepUpOwnersSettled = [...(tracker.fetchStepUpOwners?.keys?.() ?? [])]
      .every((requestId) => requestId === ignoredNetworkId);
    const preflightOwnersSettled = Number(tracker.stepUpPreflightOwners?.size ?? 0) === 0;
    const healthPreflightOwnersSettled = Number(tracker.healthPreflightOwners?.size ?? 0) === 0;
    const loginAttemptsSettled = (tracker.loginAttempts ?? [])
      .every((attempt) => attempt.state === "completed");
    if (
      requestOwnersSettled
      && fetchStepUpOwnersSettled
      && preflightOwnersSettled
      && healthPreflightOwnersSettled
      && loginAttemptsSettled
      && healthRecoveriesSettled
    ) {
      const revision = Number(tracker.networkActivityRevision ?? 0);
      if (emptySince === null || emptyRevision !== revision) {
        emptySince = Date.now();
        emptyRevision = revision;
      }
      if (Date.now() - emptySince >= stableMs) return true;
    } else {
      emptySince = null;
      emptyRevision = null;
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
  const hasPendingHealth = [...(tracker.healthRequestLedgers?.values?.() ?? [])]
    .some((ledger) => !["succeeded", "recovered", "invalid"].includes(ledger.state));
  throw new BrowserAcceptanceFailure(stage, hasPendingHealth ? "health_ledger_timeout" : "owners_timeout");
}


/** 等待网络稳定后执行真实注销，并同时证明表单返回、HTTP 200 与 owner 账本归零。 */
async function logoutRole(client, sessionId, tracker, stage) {
  await waitForRequestOwnersDrain(tracker, `${stage}.idle`);
  const expected = tracker.logoutStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('#logout-button');
    if (!button) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `${stage}.form`);
  await waitForTrackerCount(() => tracker.logoutStatuses.length, expected, `${stage}.network`, 60_000);
  requireCondition(tracker.logoutStatuses.at(-1) === 200, stage, "status");
}


/** 等待审批中心至少出现预期数量的安全摘要行，不读取任务 ID 或目标敏感正文。 */
async function waitForList(client, sessionId, expectedMinimum, stage) {
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-approvals-root][data-approval-view="center"]')
      && document.querySelectorAll('[data-approval-select]').length >= ${expectedMinimum}`,
    stage,
  );
}


/**
 * 等待 overview 的角色首页完成本轮后端刷新。
 *
 * 只等审批根节点消失不足以证明导航已稳定：全控制台刷新仍可能在后台执行，随后立即返回审批页会主动
 * 取消尚未完成的 Control 请求，并被 Chromium 记录为 ERR_FAILED。revision 只存在于成功终态，loading
 * 根节点则带 aria-busy，因此这里同时校验 hash、view 与 revision，避免把旧壳或错误态误认成完成。
 */
async function waitForOverviewStable(client, sessionId, tracker, stage) {
  await waitForDom(client, sessionId, `(() => {
    const root = document.querySelector(
      '[data-role-dashboard-root][data-role-dashboard-revision]:not([aria-busy="true"])',
    );
    return location.hash === '#overview'
      && Boolean(root?.dataset.roleDashboardView)
      && Boolean(root?.dataset.roleDashboardRevision);
  })()`, stage);
  // overview DOM 可能沿用上一轮已成功根节点；必须再证明除刻意暂停的 step-up 外，本轮全壳读取
  // 已连续稳定归零，避免随后释放迟到响应时取消 config summary 等普通 Control 请求。
  await waitForRequestOwnersDrain(tracker, `${stage}.network`, { allowPausedStepUp: true });
}


/** 通过现有返回、刷新或固定 hash 回到审批中心，并等待列表与网络账本稳定。 */
async function openCenter(client, sessionId, tracker, expectedMinimum, stage) {
  const changed = await evaluate(client, sessionId, `(() => {
    const back = document.querySelector('[data-approval-back]');
    if (back && !back.disabled) {
      back.click();
      return 'back';
    }
    const refresh = document.querySelector('[data-approval-refresh]');
    if (location.hash === '#approvals' && refresh && !refresh.disabled) {
      refresh.click();
      return 'refresh';
    }
    location.hash = '#approvals';
    return 'hash';
  })()`, stage);
  requireCondition(["back", "refresh", "hash"].includes(changed), stage, "navigate");
  await waitForList(client, sessionId, expectedMinimum, `${stage}.list`);
  await waitForRequestOwnersDrain(tracker, `${stage}.idle`);
}


/** 列表中的审批 ID 只在页面内部拼入 canonical PAGE-109 hash，不返回到 Node。 */
async function openTaskCard(client, sessionId, tracker, label, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const item = [...document.querySelectorAll('[data-approval-select]')].find((candidate) => {
      const text = candidate.textContent || '';
      return text.includes(${JSON.stringify(label)}) && text.includes('待审批');
    });
    const id = item?.dataset?.approvalSelect || '';
    if (!/^apr_[0-9a-f]{32}$/.test(id)) return false;
    location.hash = '#approvals?approval_id=' + encodeURIComponent(id);
    return true;
  })()`, stage);
  requireCondition(opened === true, stage, "task");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-approvals-root][data-approval-view="mobile-card"]')
      && document.querySelector('.approval-detail [data-approval-action]')`,
    `${stage}.detail`,
  );
  await waitForRequestOwnersDrain(tracker, `${stage}.idle`);
}


/** 桌面预算任务同为 release，按当前 pending 行顺序选择，但 ID 仍不离开页面。 */
async function selectPendingBudget(client, sessionId, stage) {
  const selected = await evaluate(client, sessionId, `(() => {
    const item = [...document.querySelectorAll('[data-approval-select]')].find((candidate) =>
      (candidate.textContent || '').includes('待审批')
    );
    if (!item) return false;
    item.click();
    return true;
  })()`, stage);
  requireCondition(selected === true, stage, "task");
  await waitForDom(client, sessionId, "document.querySelector('.approval-detail [data-approval-action]')", `${stage}.detail`);
}


/** 点击固定 approve/reject 动作并证明原生模态层打开，禁用或缺失按钮固定失败。 */
async function openActionDialog(client, sessionId, action, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-approval-action="${action}"]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(opened === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-approval-dialog]')?.matches(':modal')",
    `${stage}.modal`,
  );
}


/** 读取模态、忙碌、焦点和密码长度等布尔/计数事实，绝不返回密码或任务正文。 */
async function dialogFacts(client, sessionId) {
  // 固定页面表达式只返回布尔与长度，sessionId 绑定本轮受控页面而不进入证据。
  return evaluate(client, sessionId, `(() => {
    const dialog = document.querySelector('[data-approval-dialog]');
    const active = document.activeElement;
    return {
      native_modal: Boolean(dialog?.open && dialog.matches(':modal')),
      standard_initial_focus: Boolean(active?.matches?.('[data-approval-reason]')),
      high_risk_initial_focus: Boolean(active?.matches?.('[data-approval-password]')),
      busy: dialog?.dataset?.approvalBusy === 'true',
      password_length: document.querySelector('[data-approval-password]')?.value.length || 0,
    };
  })()`, "browser.dialog.facts");
}


/** 通过 CDP 分发成对键盘事件，验证原生焦点和 Escape 行为而不注入文本。 */
async function dispatchKey(client, sessionId, key, code, virtualKeyCode, modifiers = 0) {
  await client.send("Input.dispatchKeyEvent", {
    type: "keyDown", key, code, windowsVirtualKeyCode: virtualKeyCode, modifiers,
  }, sessionId);
  await client.send("Input.dispatchKeyEvent", {
    type: "keyUp", key, code, windowsVirtualKeyCode: virtualKeyCode, modifiers,
  }, sessionId);
}


/** 验证 Tab 与 Shift+Tab 在审批模态框内双向循环，焦点不得逃逸到背景页面。 */
async function verifyFocusTrap(client, sessionId) {
  const preparedForward = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-approval-dialog] button[type="submit"]');
    button?.focus();
    return document.activeElement === button;
  })()`, "browser.dialog.tab_forward.prepare");
  requireCondition(preparedForward === true, "browser.dialog.tab_forward", "prepare");
  await dispatchKey(client, sessionId, "Tab", "Tab", 9);
  const forward = await evaluate(client, sessionId, "document.activeElement?.matches('[data-approval-dialog-close]') === true", "browser.dialog.tab_forward");

  const preparedReverse = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-approval-dialog-close]');
    button?.focus();
    return document.activeElement === button;
  })()`, "browser.dialog.tab_reverse.prepare");
  requireCondition(preparedReverse === true, "browser.dialog.tab_reverse", "prepare");
  await dispatchKey(client, sessionId, "Tab", "Tab", 9, 8);
  const reverse = await evaluate(client, sessionId, "document.activeElement?.matches('[data-approval-dialog] button[type=\"submit\"]') === true", "browser.dialog.tab_reverse");
  return Object.freeze({ forward, reverse });
}


/** 真实鼠标点击 modal 背后的刷新坐标，确认原生 top layer 阻断背景命令。 */
async function verifyBackgroundBlocked(client, sessionId, tracker) {
  const before = tracker.listStatuses.length;
  const point = await evaluate(client, sessionId, `(() => {
    const rect = document.querySelector('[data-approval-refresh]')?.getBoundingClientRect();
    return rect ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } : null;
  })()`, "browser.dialog.background.point");
  requireCondition(point && Number.isFinite(point.x) && Number.isFinite(point.y), "browser.dialog.background", "point");
  await client.send("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 }, sessionId);
  await client.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 }, sessionId);
  await new Promise((resolve) => setTimeout(resolve, 300));
  const open = await evaluate(client, sessionId, "document.querySelector('[data-approval-dialog]')?.matches(':modal') === true", "browser.dialog.background");
  return open === true && tracker.listStatuses.length === before;
}


/** 用真实 Escape 关闭空闲 Dialog，并按场景证明密码节点擦除及焦点安全恢复。 */
async function closeIdleDialog(client, sessionId, expectPasswordClear, stage) {
  await dispatchKey(client, sessionId, "Escape", "Escape", 27);
  await waitForDom(client, sessionId, "!document.querySelector('[data-approval-dialog]')", `${stage}.closed`);
  const facts = await evaluate(client, sessionId, `(() => ({
    password_cleared: window.__prd078PasswordProbe
      ? window.__prd078PasswordProbe.value === '' && !window.__prd078PasswordProbe.isConnected
      : true,
    focus_restored: Boolean(document.activeElement?.matches?.('[data-approval-action]')),
  }))()`, stage);
  await evaluate(client, sessionId, "delete window.__prd078PasswordProbe; true", `${stage}.cleanup`);
  return Object.freeze({
    closed: true,
    passwordCleared: expectPasswordClear ? facts.password_cleared === true : true,
    focusRestored: facts.focus_restored === true,
  });
}


/** 将一次性测试密码仅写入当前 password input，并触发真实 input 事件供 step-up 消费。 */
async function fillPassword(client, sessionId, password, stage) {
  const filled = await evaluate(client, sessionId, `(() => {
    const input = document.querySelector('[data-approval-password]');
    if (!input) return false;
    input.value = ${JSON.stringify(password)};
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return input.value.length > 0;
  })()`, stage);
  requireCondition(filled === true, stage, "input");
}


/** 保留短生命周期 DOM 引用，用于证明清理发生在节点移除前；引用不进入 Storage 或证据。 */
async function installPasswordClearProbe(client, sessionId, stage) {
  const installed = await evaluate(client, sessionId, `(() => {
    const input = document.querySelector('[data-approval-password]');
    if (!input || input.value.length === 0) return false;
    window.__prd078PasswordProbe = input;
    return true;
  })()`, stage);
  requireCondition(installed === true, stage, "missing");
}


async function submitDialog(client, sessionId, stage) {
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('[data-approval-action-form]');
    if (!form) return false;
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
}


/** 等待唯一决策 HTTP 200、预期终态 DOM 与所有网络 owner 收敛，正文不进入账本。 */
async function waitForDecision(client, sessionId, tracker, previousCount, status, stage) {
  await waitForTrackerCount(() => tracker.decisionStatuses.length, previousCount + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.decisionStatuses.at(-1) === 200, stage, "status");
  await waitForDom(
    client,
    sessionId,
    `!document.querySelector('[data-approval-dialog]')
      && document.querySelector('.approval-status')?.classList.contains('tone-${status}')`,
    `${stage}.dom`,
  );
  await waitForRequestOwnersDrain(tracker, `${stage}.idle`);
}


/** 提交无需 step-up 的标准审批，并绑定本次动作前后的决策状态计数。 */
async function submitStandardDecision(client, sessionId, tracker, action, status, stage) {
  const before = tracker.decisionStatuses.length;
  await submitDialog(client, sessionId, stage);
  await waitForDecision(client, sessionId, tracker, before, status, stage);
}


/** 在同一交互内输入一次性密码、完成 step-up 与决策，并验证两条网络状态。 */
async function submitHighRiskDecision(client, sessionId, tracker, privateConfig, stage) {
  const beforeStepUp = tracker.stepUpStatuses.length;
  const beforeDecision = tracker.decisionStatuses.length;
  await fillPassword(client, sessionId, privateConfig.rolePassword, `${stage}.password`);
  await submitDialog(client, sessionId, stage);
  await waitForTrackerCount(() => tracker.stepUpStatuses.length, beforeStepUp + 1, `${stage}.step_up`, 60_000);
  requireCondition(tracker.stepUpStatuses.at(-1) === 200, `${stage}.step_up`, "status");
  await waitForDecision(client, sessionId, tracker, beforeDecision, "approved", stage);
}


/** 只读取几何和计数，不返回页面正文、ID 或身份。 */
async function readLayoutMetrics(client, sessionId) {
  return evaluate(client, sessionId, `(() => {
    const shown = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden'
        && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const root = document.querySelector('[data-approvals-root]');
    const dialog = document.querySelector('[data-approval-dialog]');
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
    return {
      width: innerWidth,
      height: innerHeight,
      root_visible: shown(root),
      root_horizontal_bounds: Boolean(rect) && rect.left >= -1 && rect.right <= innerWidth + 1,
      horizontal_overflow: document.documentElement.scrollWidth > innerWidth + 1
        || document.body.scrollWidth > innerWidth + 1,
      text_overflow: textNodes.some((node) => node.scrollWidth > node.clientWidth + 1),
      overlap_count: overlaps,
    };
  })()`, "browser.layout.read");
}


/** 校验固定视口、根边界、横向溢出、文本溢出和控件重叠均满足截图门禁。 */
function assertLayout(metrics, viewport, stage) {
  requireCondition(metrics?.width === viewport.width && metrics.height === viewport.height, stage, "viewport");
  requireCondition(metrics.root_visible === true && metrics.root_horizontal_bounds === true, stage, "root");
  requireCondition(metrics.horizontal_overflow === false, stage, "horizontal_overflow");
  requireCondition(metrics.text_overflow === false, stage, "text_overflow");
  requireCondition(metrics.overlap_count === 0, stage, "overlap");
  return metrics;
}


/** 截图前只隐藏用户身份菜单；审批交互本身保持真实可见。 */
async function captureScreenshot(client, sessionId, staging, filename, viewport, stage) {
  await setViewport(client, sessionId, viewport);
  await settle(client, sessionId, `${stage}.settle`);
  const layout = assertLayout(await readLayoutMetrics(client, sessionId), viewport, `${stage}.layout`);
  const masked = await evaluate(client, sessionId, `(() => {
    const menu = document.querySelector('.user-menu');
    if (!menu) return false;
    menu.dataset.prd078Mask = 'true';
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
      const menu = document.querySelector('[data-prd078-mask="true"]');
      if (!menu) return false;
      menu.style.visibility = '';
      delete menu.dataset.prd078Mask;
      return true;
    })()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  const pixels = inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({ layout, pixels });
}


/** 扫描 DOM 与浏览器持久存储的脱敏计数，密码、Token 和正文绝不作为返回字段。 */
async function privacyFacts(client, sessionId, privateConfig, { expectRoot, expectAuthenticated }) {
  // 页面表达式仅返回存在性和计数，sessionId 绑定当前页面且不会写入最终事实对象。
  return evaluate(client, sessionId, `(async () => {
    const privateValues = [${JSON.stringify(privateConfig.rolePassword)}];
    const text = document.documentElement.textContent || '';
    const attributes = [...document.querySelectorAll('*')].flatMap((element) =>
      [...element.attributes].map((attribute) => attribute.value)
    );
    const values = [...document.querySelectorAll('input,textarea,select')].map((field) => String(field.value || ''));
    const cacheNames = typeof caches === 'undefined' ? [] : await caches.keys();
    const databases = typeof indexedDB.databases === 'function' ? await indexedDB.databases() : [];
    return {
      root: Boolean(document.querySelector('[data-approvals-root]')),
      dialog: Boolean(document.querySelector('[data-approval-dialog]')),
      password_hits: privateValues.filter((needle) => text.includes(needle)
        || attributes.some((value) => value.includes(needle))
        || values.some((value) => value.includes(needle))).length,
      local_storage_count: localStorage.length,
      session_storage_count: sessionStorage.length,
      cache_count: cacheNames.length,
      indexeddb_count: databases.length,
    };
  })()`, "browser.privacy").then((facts) => {
    requireCondition(facts.root === expectRoot, "browser.privacy", "root");
    requireCondition(facts.password_hits === 0, "browser.privacy", "password");
    requireCondition(facts.cache_count === 0 && facts.indexeddb_count === 0, "browser.privacy", "persistent_store");
    // 注销场景必须同时清空 local/session Storage，登录场景只验证禁止的持久存储类型。
    if (!expectAuthenticated) {
      requireCondition(
        facts.local_storage_count === 0 && facts.session_storage_count === 0,
        "browser.privacy",
        "auth_storage",
      );
    }
    return facts;
  });
}


/** 财务角色只见预算任务，并完成原生标准 modal、批准和拒绝。 */
async function exerciseFinance(client, sessionId, tracker, privateConfig, staging) {
  tracker.phase = "finance";
  await setViewport(client, sessionId, VIEWPORTS.desktop);
  await loginRole(client, sessionId, "finance", privateConfig, tracker);
  await waitForList(client, sessionId, 3, "browser.finance.list");
  const financeOnly = await evaluate(client, sessionId, `(() => {
    const items = [...document.querySelectorAll('[data-approval-select]')];
    return items.length === 3 && items.every((item) =>
      (item.textContent || '').includes('预算阻断释放')
      && !(item.textContent || '').includes('会话风险处置')
    );
  })()`, "browser.finance.scope");
  requireCondition(financeOnly === true, "browser.finance.scope", "target");

  await selectPendingBudget(client, sessionId, "browser.finance.select_approve");
  await openActionDialog(client, sessionId, "approve", "browser.finance.dialog");
  const dialog = await dialogFacts(client, sessionId);
  requireCondition(dialog.native_modal && dialog.standard_initial_focus, "browser.finance.dialog", "focus");
  const backgroundBlocked = await verifyBackgroundBlocked(client, sessionId, tracker);
  const trap = await verifyFocusTrap(client, sessionId);
  const idle = await closeIdleDialog(client, sessionId, false, "browser.finance.escape");
  requireCondition(idle.focusRestored, "browser.finance.escape", "focus");

  await openActionDialog(client, sessionId, "approve", "browser.finance.dialog_reopen");
  const screenshot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[0],
    VIEWPORTS.desktop,
    "browser.finance.screenshot",
  );
  await submitStandardDecision(client, sessionId, tracker, "approve", "approved", "browser.finance.approve");

  await selectPendingBudget(client, sessionId, "browser.finance.select_reject");
  await openActionDialog(client, sessionId, "reject", "browser.finance.reject_dialog");
  await submitStandardDecision(client, sessionId, tracker, "reject", "rejected", "browser.finance.reject");
  await logoutRole(client, sessionId, tracker, "browser.finance.logout");
  await privacyFacts(client, sessionId, privateConfig, { expectRoot: false, expectAuthenticated: false });

  return Object.freeze({
    screenshot,
    financeOnly,
    nativeModal: dialog.native_modal,
    backgroundBlocked,
    standardInitialFocus: dialog.standard_initial_focus,
    tabForwardWrap: trap.forward === true,
    tabReverseWrap: trap.reverse === true,
    idleEscapeClosed: idle.closed,
    focusRestored: idle.focusRestored,
  });
}


/** 验证高风险 idle/busy modal、迟到成功、真实登出、真实撤权 403 与迟到拒绝。 */
async function exerciseSecurity(client, sessionId, tracker, privateConfig, staging) {
  tracker.phase = "security_start";
  await setViewport(client, sessionId, VIEWPORTS.mobile);
  await loginRole(client, sessionId, "security", privateConfig, tracker);
  await waitForList(client, sessionId, 3, "browser.security.list");
  const securityOnly = await evaluate(client, sessionId, `(() => {
    const items = [...document.querySelectorAll('[data-approval-select]')];
    return items.length === 3 && items.every((item) =>
      (item.textContent || '').includes('会话风险处置')
      && !(item.textContent || '').includes('预算阻断释放')
    );
  })()`, "browser.security.scope");
  requireCondition(securityOnly === true, "browser.security.scope", "target");

  await openTaskCard(client, sessionId, tracker, "冻结会话", "browser.security.card");
  await openActionDialog(client, sessionId, "approve", "browser.security.idle_dialog");
  const idleDialog = await dialogFacts(client, sessionId);
  requireCondition(idleDialog.native_modal && idleDialog.high_risk_initial_focus, "browser.security.idle_dialog", "focus");
  await fillPassword(client, sessionId, privateConfig.rolePassword, "browser.security.escape_password");
  await installPasswordClearProbe(client, sessionId, "browser.security.escape_probe");
  const securityIdle = await closeIdleDialog(client, sessionId, true, "browser.security.escape");
  const idlePrivacy = await privacyFacts(client, sessionId, privateConfig, { expectRoot: true, expectAuthenticated: true });

  await openActionDialog(client, sessionId, "approve", "browser.security.capture_dialog");
  const idleScreenshot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[1],
    VIEWPORTS.mobile,
    "browser.security.idle_screenshot",
  );

  tracker.phase = "late_success";
  const delayedSuccess = pauseNextStepUp(tracker, 200, "browser.security.late_success");
  await fillPassword(client, sessionId, privateConfig.rolePassword, "browser.security.busy_password");
  await submitDialog(client, sessionId, "browser.security.busy_submit");
  await delayedSuccess;
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-approval-dialog][data-approval-busy=\"true\"]')?.matches(':modal')",
    "browser.security.busy_dialog",
  );
  const busyDialog = await dialogFacts(client, sessionId);
  requireCondition(busyDialog.native_modal && busyDialog.busy && busyDialog.password_length === 0, "browser.security.busy_dialog", "state");
  const busyScreenshot = await captureScreenshot(
    client,
    sessionId,
    staging,
    SCREENSHOTS[2],
    VIEWPORTS.mobile,
    "browser.security.busy_screenshot",
  );
  await dispatchKey(client, sessionId, "Escape", "Escape", 27);
  const escapeBlocked = await evaluate(client, sessionId, "document.querySelector('[data-approval-dialog][data-approval-busy=\"true\"]')?.matches(':modal') === true", "browser.security.busy_escape");
  const cancelBlocked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-approval-dialog-close]:not(.icon-command)');
    const dialog = document.querySelector('[data-approval-dialog]');
    button?.click();
    return Boolean(button?.disabled && dialog?.matches(':modal'));
  })()`, "browser.security.busy_cancel");

  const decisionCountBeforeDrift = tracker.decisionStatuses.length;
  const drifted = await evaluate(client, sessionId, `(() => {
    location.hash = '#overview';
    return true;
  })()`, "browser.security.owner_drift");
  requireCondition(drifted === true, "browser.security.owner_drift", "navigate");
  await waitForDom(client, sessionId, "!document.querySelector('[data-approvals-root]')", "browser.security.owner_drift.closed");
  await waitForOverviewStable(
    client,
    sessionId,
    tracker,
    "browser.security.owner_drift.overview_stable",
  );
  const driftFacts = await privacyFacts(client, sessionId, privateConfig, { expectRoot: false, expectAuthenticated: true });
  const focusNotRestored = await evaluate(client, sessionId, "!document.activeElement?.matches?.('[data-approval-action]')", "browser.security.owner_drift.focus");
  await releasePausedResponse(client, tracker, "browser.security.late_success.release");
  // overview 角色卡片可能早于整轮 Console refresh 完成；迟到响应释放后必须等全部回环请求稳定收敛，
  // 再返回 approvals，避免下一次导航取消后续 Control 读取并制造 ERR_FAILED。
  await waitForRequestOwnersDrain(tracker, "browser.security.late_success.idle");
  requireCondition(tracker.decisionStatuses.length === decisionCountBeforeDrift, "browser.security.late_success", "decision_started");

  tracker.phase = "late_success_return";
  const reopened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-console-nav="approvals"]');
    if (!button) return false;
    button.click();
    return true;
  })()`, "browser.security.return");
  requireCondition(reopened === true, "browser.security.return", "navigation");
  await waitForList(client, sessionId, 3, "browser.security.return.list");
  await openTaskCard(client, sessionId, tracker, "冻结会话", "browser.security.complete_card");
  const stillPending = await evaluate(client, sessionId, "document.querySelector('.approval-status')?.classList.contains('tone-pending') === true", "browser.security.late_success.pending");
  requireCondition(stillPending === true, "browser.security.late_success", "adopted");
  await openActionDialog(client, sessionId, "approve", "browser.security.complete_dialog");
  tracker.phase = "security_decision";
  await submitHighRiskDecision(client, sessionId, tracker, privateConfig, "browser.security.complete");

  tracker.phase = "security_logout_prepare";
  await openCenter(client, sessionId, tracker, 2, "browser.security.logout_center");
  await openTaskCard(client, sessionId, tracker, "冻结虚拟 Key", "browser.security.logout_card");
  await openActionDialog(client, sessionId, "approve", "browser.security.logout_dialog");
  await fillPassword(client, sessionId, privateConfig.rolePassword, "browser.security.logout_password");
  await installPasswordClearProbe(client, sessionId, "browser.security.logout_probe");
  tracker.phase = "security_logout";
  await logoutRole(client, sessionId, tracker, "browser.security.logout");
  const logoutProbe = await evaluate(client, sessionId, `(() => {
    const probe = window.__prd078PasswordProbe;
    const cleared = Boolean(probe && probe.value === '' && !probe.isConnected);
    delete window.__prd078PasswordProbe;
    return cleared;
  })()`, "browser.security.logout_probe.result");
  const logoutPrivacy = await privacyFacts(client, sessionId, privateConfig, { expectRoot: false, expectAuthenticated: false });

  tracker.phase = "security_relogin";
  await loginRole(client, sessionId, "security", privateConfig, tracker);
  await waitForList(client, sessionId, 2, "browser.security.relogin.list");
  tracker.phase = "security_revoke_prepare";
  await openTaskCard(client, sessionId, tracker, "忽略风险", "browser.security.revoke_card");
  await openActionDialog(client, sessionId, "approve", "browser.security.revoke_dialog");
  await fillPassword(client, sessionId, privateConfig.rolePassword, "browser.security.revoke_password");
  const revoke = await postHarness("/revoke-security", privateConfig);
  requireCondition(revoke.status === 200, "browser.security.revoke", "status");
  tracker.phase = "late_denial";
  const delayedDenial = pauseNextStepUp(tracker, 403, "browser.security.late_denial");
  await submitDialog(client, sessionId, "browser.security.revoked_submit");
  await delayedDenial;
  const revokeBusy = await dialogFacts(client, sessionId);
  requireCondition(revokeBusy.password_length === 0, "browser.security.revoke", "password");
  await evaluate(client, sessionId, "location.hash = '#overview'; true", "browser.security.revoke_drift");
  await waitForDom(client, sessionId, "!document.querySelector('[data-approvals-root]')", "browser.security.revoke.closed");
  await waitForOverviewStable(
    client,
    sessionId,
    tracker,
    "browser.security.revoke.overview_stable",
  );
  const revokePrivacy = await privacyFacts(client, sessionId, privateConfig, { expectRoot: false, expectAuthenticated: true });
  await releasePausedResponse(client, tracker, "browser.security.late_denial.release");
  await waitForRequestOwnersDrain(tracker, "browser.security.late_denial.idle");
  const deniedDiscarded = await evaluate(client, sessionId, `(() =>
    !document.querySelector('[data-approvals-root]')
    && !document.querySelector('[data-approval-dialog]')
    && !document.querySelector('.approval-error')
  )()`, "browser.security.late_denial.discarded");
  requireCondition(deniedDiscarded === true, "browser.security.late_denial", "adopted");
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-console-nav="overview"]');
    const root = document.querySelector('[data-role-dashboard-root]');
    if (!button || !root) return false;
    window.__prd078OverviewRefreshRoot = root;
    button.click();
    return true;
  })()`, "browser.security.revoke_refresh");
  requireCondition(refreshed === true, "browser.security.revoke_refresh", "navigation");
  await waitForDom(
    client,
    sessionId,
    `(() => {
      const root = document.querySelector(
        '[data-role-dashboard-root][data-role-dashboard-revision]:not([aria-busy="true"])',
      );
      return location.hash === '#overview'
        && Boolean(window.__prd078OverviewRefreshRoot)
        && root !== window.__prd078OverviewRefreshRoot
        && Boolean(root?.dataset.roleDashboardView)
        && Boolean(root?.dataset.roleDashboardRevision)
        && !document.querySelector('[data-console-nav="approvals"]');
    })()`,
    "browser.security.revoke_refresh.navigation",
  );
  await evaluate(
    client,
    sessionId,
    "delete window.__prd078OverviewRefreshRoot; true",
    "browser.security.revoke_refresh.cleanup",
  );
  await waitForRequestOwnersDrain(tracker, "browser.security.revoke_refresh.idle");
  tracker.phase = "security_final";
  const revokedNavigationPrivacy = await privacyFacts(
    client,
    sessionId,
    privateConfig,
    { expectRoot: false, expectAuthenticated: true },
  );
  await logoutRole(client, sessionId, tracker, "browser.security.final_logout");
  await privacyFacts(client, sessionId, privateConfig, { expectRoot: false, expectAuthenticated: false });

  return Object.freeze({
    idleScreenshot,
    busyScreenshot,
    securityOnly,
    nativeModal: idleDialog.native_modal,
    highRiskInitialFocus: idleDialog.high_risk_initial_focus,
    idleEscapeClosed: true,
    idleEscapePasswordCleared: securityIdle.passwordCleared && idlePrivacy.password_hits === 0,
    busyNativeModal: busyDialog.native_modal,
    busyEscapeBlocked: escapeBlocked === true,
    busyCancelBlocked: cancelBlocked === true,
    stepUpCompleted: true,
    ownerDriftClosed: driftFacts.root === false && driftFacts.dialog === false,
    ownerDriftPasswordCleared: driftFacts.password_hits === 0,
    ownerDriftFocusNotRestored: focusNotRestored === true,
    lateSuccessDiscarded: stillPending === true,
    permissionRevocationClosed: revokePrivacy.dialog === false
      && revokedNavigationPrivacy.dialog === false,
    permissionRevocationPasswordCleared: revokePrivacy.password_hits === 0,
    permissionRevocationRootCleared: revokePrivacy.root === false
      && revokedNavigationPrivacy.root === false,
    lateDenialDiscarded: deniedDiscarded === true,
    logoutDialogClosed: logoutPrivacy.dialog === false,
    logoutPasswordCleared: logoutProbe === true && logoutPrivacy.password_hits === 0,
    logoutRootCleared: logoutPrivacy.root === false,
    logoutStorageCleared: logoutPrivacy.local_storage_count === 0
      && logoutPrivacy.session_storage_count === 0,
  });
}


/** 把真实场景结果收敛为 Python harness 接受的 exact schema。 */
function buildCompletionFacts(finance, security, tracker) {
  const facts = {
    status: "passed",
    desktop: {
      width: 1280,
      height: 720,
      nonblank: finance.screenshot.pixels.non_blank_pixels === true,
      horizontal_overflow: finance.screenshot.layout.horizontal_overflow,
      text_overflow: finance.screenshot.layout.text_overflow,
      overlap_count: finance.screenshot.layout.overlap_count,
      finance_budget_only: finance.financeOnly,
      native_modal: finance.nativeModal,
      background_blocked: finance.backgroundBlocked,
      standard_initial_focus: finance.standardInitialFocus,
      tab_forward_wrap: finance.tabForwardWrap,
      tab_reverse_wrap: finance.tabReverseWrap,
      idle_escape_closed: finance.idleEscapeClosed,
      focus_restored: finance.focusRestored,
      decision_completed: true,
    },
    mobile: {
      width: 390,
      height: 844,
      nonblank: security.idleScreenshot.pixels.non_blank_pixels === true
        && security.busyScreenshot.pixels.non_blank_pixels === true,
      horizontal_overflow: security.idleScreenshot.layout.horizontal_overflow
        || security.busyScreenshot.layout.horizontal_overflow,
      text_overflow: security.idleScreenshot.layout.text_overflow
        || security.busyScreenshot.layout.text_overflow,
      overlap_count: Math.max(
        security.idleScreenshot.layout.overlap_count,
        security.busyScreenshot.layout.overlap_count,
      ),
      security_risk_only: security.securityOnly,
      native_modal: security.nativeModal,
      high_risk_initial_focus: security.highRiskInitialFocus,
      idle_escape_closed: security.idleEscapeClosed,
      idle_escape_password_cleared: security.idleEscapePasswordCleared,
      busy_native_modal: security.busyNativeModal,
      busy_escape_blocked: security.busyEscapeBlocked,
      busy_cancel_blocked: security.busyCancelBlocked,
      step_up_completed: security.stepUpCompleted,
    },
    lifecycle: {
      owner_drift_closed: security.ownerDriftClosed,
      owner_drift_password_cleared: security.ownerDriftPasswordCleared,
      owner_drift_focus_not_restored: security.ownerDriftFocusNotRestored,
      late_success_discarded: security.lateSuccessDiscarded,
      permission_revocation_closed: security.permissionRevocationClosed,
      permission_revocation_password_cleared: security.permissionRevocationPasswordCleared,
      permission_revocation_root_cleared: security.permissionRevocationRootCleared,
      late_denial_discarded: security.lateDenialDiscarded,
      logout_dialog_closed: security.logoutDialogClosed,
      logout_password_cleared: security.logoutPasswordCleared,
      logout_root_cleared: security.logoutRootCleared,
      logout_storage_cleared: security.logoutStorageCleared,
    },
    network: {
      external_requests: tracker.externalRequests,
      unexpected_console_errors: tracker.unexpectedConsoleErrors,
      runtime_exceptions: tracker.runtimeExceptions,
    },
    screenshots: [...SCREENSHOTS],
  };
  return validateCompletionFacts(facts);
}


/** Runner 自身先执行与 harness 等价的严格门禁，避免提交近似事实。 */
function validateCompletionFacts(facts) {
  const desktopKeys = [
    "background_blocked", "decision_completed", "finance_budget_only", "focus_restored",
    "height", "horizontal_overflow", "idle_escape_closed", "native_modal", "nonblank",
    "overlap_count", "standard_initial_focus", "tab_forward_wrap", "tab_reverse_wrap",
    "text_overflow", "width",
  ];
  const mobileKeys = [
    "busy_cancel_blocked", "busy_escape_blocked", "busy_native_modal", "height",
    "high_risk_initial_focus", "horizontal_overflow", "idle_escape_closed",
    "idle_escape_password_cleared", "native_modal", "nonblank", "overlap_count",
    "security_risk_only", "step_up_completed", "text_overflow", "width",
  ];
  const trueDesktop = [
    "background_blocked", "decision_completed", "finance_budget_only", "focus_restored",
    "idle_escape_closed", "native_modal", "nonblank", "standard_initial_focus",
    "tab_forward_wrap", "tab_reverse_wrap",
  ];
  const trueMobile = [
    "busy_cancel_blocked", "busy_escape_blocked", "busy_native_modal",
    "high_risk_initial_focus", "idle_escape_closed", "idle_escape_password_cleared",
    "native_modal", "nonblank", "security_risk_only", "step_up_completed",
  ];
  /** 比较完成信号的 exact 顶层键集合，拒绝夹带动态 ID、凭据或未审计字段。 */
  const keysEqual = (value, expected) => JSON.stringify(Object.keys(value ?? {}).sort())
    === JSON.stringify([...expected].sort());
  requireCondition(
    keysEqual(facts, ["status", "desktop", "mobile", "lifecycle", "network", "screenshots"])
      && facts?.status === "passed"
      && keysEqual(facts.desktop, desktopKeys)
      && keysEqual(facts.mobile, mobileKeys)
      && facts.desktop?.width === 1280
      && facts.desktop?.height === 720
      && facts.mobile?.width === 390
      && facts.mobile?.height === 844
      && trueDesktop.every((name) => facts.desktop[name] === true)
      && trueMobile.every((name) => facts.mobile[name] === true)
      && facts.desktop.horizontal_overflow === false
      && facts.desktop.text_overflow === false
      && facts.desktop.overlap_count === 0
      && facts.mobile.horizontal_overflow === false
      && facts.mobile.text_overflow === false
      && facts.mobile.overlap_count === 0
      && keysEqual(facts.lifecycle, [
        "late_denial_discarded", "late_success_discarded", "logout_dialog_closed",
        "logout_password_cleared", "logout_root_cleared", "logout_storage_cleared",
        "owner_drift_closed", "owner_drift_focus_not_restored",
        "owner_drift_password_cleared", "permission_revocation_closed",
        "permission_revocation_password_cleared", "permission_revocation_root_cleared",
      ])
      && Object.values(facts.lifecycle ?? {}).every((value) => value === true)
      && keysEqual(facts.network, ["external_requests", "unexpected_console_errors", "runtime_exceptions"])
      && facts.network?.external_requests === 0
      && facts.network?.unexpected_console_errors === 0
      && facts.network?.runtime_exceptions === 0
      && JSON.stringify(facts.screenshots) === JSON.stringify(SCREENSHOTS),
    "browser.complete",
    "contract",
  );
  return facts;
}


/** 汇总网络、隐私、登录、取消、Console 和业务状态账本；任何残留 owner 都拒绝完成。 */
function assertTracker(tracker) {
  requireCondition(tracker.externalRequests === 0, "browser.network", "external");
  requireCondition(tracker.forbiddenQueries === 0, "browser.network", "query");
  requireCondition(tracker.requestPrivacyViolations === 0, "browser.network", "privacy");
  requireCondition(tracker.processPrivacyViolations === 0, "browser.process", "privacy");
  requireCondition(tracker.processPrivacyViolationCount === 0, "browser.chrome", "privacy");
  requireCondition(tracker.requestOwners.size === 0, "browser.network", "owners_remaining");
  requireCondition(
    (tracker.loginAttempts ?? []).length === 3
      && tracker.loginAttempts.every((attempt) => attempt.state === "completed"),
    "browser.network",
    "login_attempts",
  );
  requireCondition(
    Number(tracker.fetchStepUpOwners?.size ?? 0) === 0,
    "browser.network",
    "fetch_step_up_owners_remaining",
  );
  requireCondition(
    Number(tracker.stepUpPreflightOwners?.size ?? 0) === 0,
    "browser.network",
    "step_up_preflight_owners_remaining",
  );
  requireCondition(
    Number(tracker.healthPreflightOwners?.size ?? 0) === 0,
    "browser.network",
    "health_preflight_owners_remaining",
  );
  assertCancellationLedger(tracker);
  // 非预期 Console 错误只输出首个固定分类坐标，绝不传播原始消息或 URL。
  if (tracker.unexpectedConsoleErrors !== 0) {
    const reason = [...tracker.unexpectedConsoleReasons.keys()].sort()[0] ?? "unknown";
    throw new BrowserAcceptanceFailure("browser.console", `unexpected_${reason}`);
  }
  assertStepUpConsoleLedger(tracker);
  requireCondition(tracker.runtimeExceptions === 0, "browser.runtime", "exception");
  requireCondition(tracker.browserProcessErrors === 0, "browser.cdp", "listener");
  requireCondition(tracker.edgeProcessErrorCount === 0, "browser.chrome", "process");
  requireCondition(
    tracker.decisionStatuses.filter((status) => status === 200).length === 3
      && tracker.stepUpStatuses.includes(200)
      && JSON.stringify(tracker.interceptedStepUpStatuses) === JSON.stringify([200, 403]),
    "browser.network",
    "business_statuses",
  );
}


/** Fetch 精确证明 403；Chromium Console 可能省略对应资源日志，但不得报告额外 403。 */
function assertStepUpConsoleLedger(tracker) {
  const interceptedDenials = (tracker.interceptedStepUpStatuses ?? [])
    .filter((status) => status === 403).length;
  requireCondition(interceptedDenials === 1, "browser.network", "step_up_denial_count");
  requireCondition(
    Number(tracker.expectedStepUpConsoleErrors ?? 0) <= interceptedDenials,
    "browser.console",
    "step_up_403_count_over",
  );
  return true;
}


/** 两次 owner drift 与健康重试必须由 Network 精确证明；无 ID Console 候选不得超出该账本。 */
function assertCancellationLedger(tracker) {
  throwUnexpectedNetworkFailure(tracker);
  requireCondition(
    tracker.expectedCanceledRequestIds.size === 0,
    "browser.network",
    "expected_cancel_remaining",
  );
  requireCondition(
    Number(tracker.fetchStepUpOwners?.size ?? 0) === 0,
    "browser.network",
    "fetch_step_up_owners_remaining",
  );
  requireCondition(
    Number(tracker.stepUpPreflightOwners?.size ?? 0) === 0,
    "browser.network",
    "step_up_preflight_owners_remaining",
  );
  requireCondition(
    Number(tracker.healthPreflightOwners?.size ?? 0) === 0,
    "browser.network",
    "health_preflight_owners_remaining",
  );
  requireCondition(
    tracker.canceledStepUpRequestCount === 2,
    "browser.network",
    "step_up_cancel_count",
  );
  requireCondition(
    tracker.healthProbeFailureCount === tracker.healthProbeRecoveryCount
      && [...tracker.healthRequestLedgers.values()]
        .every((ledger) => ["succeeded", "recovered"].includes(ledger.state)),
    "browser.network",
    "health_recovery",
  );
  const expectedConsoleNetworkFailures = tracker.canceledStepUpRequestCount
    + tracker.healthProbeFailureCount;
  if (tracker.consoleNetworkFailureCount > expectedConsoleNetworkFailures) {
    throw new BrowserAcceptanceFailure("browser.console", "network_failure_count_over");
  }
  requireCondition(
    tracker.consoleNetworkFailureCount <= expectedConsoleNetworkFailures,
    "browser.console",
    "network_cancel_reconciliation",
  );
  return true;
}


/** 从调用时观察 close；该事件同时证明父进程退出且所有继承的 stdio 均已 EOF。 */
function observeChildDrain(child) {
  requireCondition(child && typeof child.once === "function", "cleanup.process_tree", "child");
  const existing = CHILD_DRAIN_STATES.get(child);
  if (existing) return existing;
  let resolveClose;
  const state = {
    closeObserved: false,
    promise: new Promise((resolve) => { resolveClose = resolve; }),
  };
  CHILD_DRAIN_STATES.set(child, state);
  child.once("close", (code) => {
    state.closeObserved = true;
    resolveClose(code);
  });
  const streams = [child.stdout, child.stderr].filter(Boolean);
  if (
    child.exitCode !== null
    && streams.every((stream) => stream.readableEnded === true || stream.closed === true)
  ) {
    state.closeObserved = true;
    resolveClose(child.exitCode);
  }
  return state;
}


/** 等待精确子进程 close 与继承 stdio EOF，超时则拒绝声称进程清理完成。 */
async function waitForChildDrain(child, timeoutMs = 10_000) {
  const state = observeChildDrain(child);
  if (state.closeObserved) return;
  let timer;
  try {
    await Promise.race([
      state.promise,
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new BrowserAcceptanceFailure("cleanup.process_tree", "close_timeout")),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}


/** Windows 只接受 launchEdge 返回的精确父 PID，并由 taskkill 回收其整个一次性子树。 */
async function terminateWindowsProcessTree(
  pid,
  { spawnOperation = spawn, timeoutMs = 10_000 } = {},
) {
  requireCondition(Number.isInteger(pid) && pid > 0, "cleanup.process_tree", "pid");
  await new Promise((resolve, reject) => {
    let killer;
    try {
      killer = spawnOperation("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
    } catch {
      reject(new BrowserAcceptanceFailure("cleanup.process_tree", "spawn"));
      return;
    }
    let settled = false;
    /** 只允许 taskkill 进程结算一次，并在成功或固定失败时统一清除超时器。 */
    const finish = (error = null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      // 清理错误只传播固定验收异常；成功分支不返回 taskkill 输出或宿主进程信息。
      if (error) reject(error); else resolve();
    };
    const timer = setTimeout(() => {
      try { if (killer.exitCode === null) killer.kill(); } catch {}
      finish(new BrowserAcceptanceFailure("cleanup.process_tree", "timeout"));
    }, timeoutMs);
    killer.once("error", () => {
      finish(new BrowserAcceptanceFailure("cleanup.process_tree", "spawn"));
    });
    killer.once("exit", (code) => {
      finish(code === 0 ? null : new BrowserAcceptanceFailure("cleanup.process_tree", "exit"));
    });
  });
}


/** 仅回收本 runner 创建的精确浏览器父进程树，并等待 stdio 完整排空。 */
async function terminateProcessTree(child, treeOptions = {}) {
  if (!child) return;
  observeChildDrain(child);
  if (child.exitCode === null && process.platform === "win32") {
    await terminateWindowsProcessTree(Number(child.pid), treeOptions);
  } else if (child.exitCode === null) {
    child.kill("SIGKILL");
  }
  await waitForChildDrain(child, 10_000);
  child.unref();
}


/**
 * Windows 的 crashpad/renderer 会继承 stdout/stderr；Browser.close 可能先结束父进程，留下
 * 无法再按父 PID 回收的后代。因此页面事实落盘后直接回收本 runner 创建的精确进程树。
 */
async function closeBrowser(
  browserClient,
  pageSession,
  child,
  {
    platform = process.platform,
    terminateOperation = terminateProcessTree,
  } = {},
) {
  try { pageSession?.client?.close(); } catch {}
  if (platform === "win32" && child?.exitCode === null) {
    try { browserClient?.close(); } catch {}
    await terminateOperation(child);
    return Object.freeze({ treeDrained: true, stdioEof: true, forced: true });
  }
  if (browserClient) {
    try { await browserClient.send("Browser.close", {}, undefined, 5000); } catch {}
    browserClient.close();
  }
  if (child) await terminateOperation(child);
  return Object.freeze({ treeDrained: true, stdioEof: true, forced: child !== null });
}


/** 编排单批次 harness、Chrome、真实交互、证据发布与 owner-safe 清理。 */
async function run(runId) {
  requireCondition(existsSync(PYTHON) && existsSync(HARNESS) && existsSync(CHROME), "config.runtime", "missing");
  await assertPortFree(DEBUG_PORT);
  const privateConfig = createPrivateConfiguration();
  const tracker = createTracker(privateConfig);
  const driverDirectory = await createDriverDirectory(runId);
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
    browserChild.once("error", () => { tracker.browserProcessErrors += 1; });
    const websocket = await waitForDebugger(DEBUG_PORT, 90_000);
    browserClient = new CdpClient(websocket);
    await browserClient.connect();
    pageSession = await createPageSession(browserClient, DEBUG_PORT);
    await bindBrowserGuards(pageSession.client, tracker);
    const clockGateIdentifier = await installBrowserClockGate(
      pageSession.client,
      pageSession.sessionId,
    );
    await loadConsole(pageSession.client, pageSession.sessionId);
    await restoreBrowserClockGate(
      pageSession.client,
      pageSession.sessionId,
      clockGateIdentifier,
    );

    const finance = await exerciseFinance(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      privateConfig,
      staging,
    );
    const security = await exerciseSecurity(
      pageSession.client,
      pageSession.sessionId,
      tracker,
      privateConfig,
      staging,
    );
    assertTracker(tracker);
    const facts = buildCompletionFacts(finance, security, tracker);

    await closeBrowser(browserClient, pageSession, browserChild);
    browserClient = null;
    pageSession = null;
    browserChild = null;
    await removeDriverDirectory(driverDirectory);

    const completed = await postHarness("/complete", privateConfig, facts);
    requireCondition(completed.status === 200, "harness.complete", "status");
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-078", screenshot_count: 3 });
  } catch (error) {
    primaryFailure = safeFailure(error);
    throw primaryFailure;
  } finally {
    let cleanupFailure = null;
    try { await closeBrowser(browserClient, pageSession, browserChild); } catch {
      cleanupFailure = new BrowserAcceptanceFailure("cleanup.browser", "failed");
    }
    try { await removeDriverDirectory(driverDirectory); } catch {
      cleanupFailure ??= new BrowserAcceptanceFailure("cleanup.driver_directory", "failed");
    }
    if (harness?.child?.exitCode === null) await abortHarness(harness, privateConfig);
    if (!primaryFailure && cleanupFailure) throw cleanupFailure;
  }
}


/** CLI 入口只输出固定成功 schema 或脱敏失败坐标，并返回对应退出码。 */
async function main() {
  try {
    const { runId } = parseArguments();
    const result = await run(runId);
    // stdout/stderr 可能仍被已关闭的 CDP 内部句柄引用；同步写固定 schema 后由 CLI 显式退出。
    writeSync(process.stdout.fd, `${JSON.stringify(result)}\n`);
    return 0;
  } catch (error) {
    const failure = safeFailure(error);
    writeSync(
      process.stderr.fd,
      `${JSON.stringify({ status: "failed", stage: failure.stage, code: failure.code })}\n`,
    );
    return 1;
  }
}


export {
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
  recordLoginNetworkResponse,
  recordLoginNetworkTerminal,
  registerHealthPreflightRequest,
  registerFetchStepUpOwner,
  registerPausedStepUpResponse,
  registerStepUpPreflightRequest,
  registerStepUpPreflightResponse,
  restoreBackgroundRefreshClockGate,
  safeRunId,
  terminateProcessTree,
  terminateWindowsProcessTree,
  validateCompletionFacts,
  validateBackgroundRefreshClockGateFacts,
  waitForLoginCompletion,
  waitForRequestOwnersDrain,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
