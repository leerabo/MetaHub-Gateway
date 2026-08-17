/** 驱动真实 Chromium 完成 PRD-085 PAGE-117 浏览器验收与隐私门禁。 */

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, writeSync } from "node:fs";
import { link, lstat, mkdir, readFile, readdir, rm, rmdir, unlink, writeFile } from "node:fs/promises";
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

// 固定仓库路径、运行工具、回环地址、输入规则和截图清单，限定浏览器验收边界。
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd085-browser-harness.py");
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const DRIVER_ROOT = path.join(ROOT, ".tmp", "prd085-browser-driver");
const BROWSER_RUNTIME_ROOT = path.join(ROOT, ".tmp", "prd085-browser-runtime");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd085-browser");
const DRIVER_OWNER_FILENAME = ".prd085-browser-driver-owner";
const BROWSER_RUNTIME_OWNER_FILENAME = ".prd085-browser-runtime-owner";
const EXCLUDED_BROWSER_RUNTIME_FILES = new Set(["debug.log", "os_update_handler.exe"]);
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const HARNESS_ORIGIN = "http://127.0.0.1:18887";
const DEBUG_PORT = 19087;
const DOM_WAIT_TIMEOUT_MS = 180_000;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const FORBIDDEN_QUERY_KEY = /(?:tenant|password|token|secret|authorization|prompt|response|key|endpoint)/i;
const OPS_EMAIL = "ops-prd085@local.invalid";
const DEVELOPER_EMAIL = "developer-prd085@local.invalid";
const SECONDARY_EMAIL = "secondary-prd085@local.invalid";
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "page-117-growth-analytics-desktop-1280x720.png",
  "page-117-growth-analytics-mobile-390x844.png",
  "page-117-initial-state-desktop-1280x720.png",
  "page-117-tenant-isolation-mobile-390x844.png",
]);
// 仅允许控制台、控制面、网关和协调服务的本机来源，所有外部请求都必须拦截。
const ALLOWED_ORIGINS = new Set([
  CONSOLE_ORIGIN,
  CONTROL_ORIGIN,
  "http://127.0.0.1:5173",
  "http://127.0.0.1:8001",
  "http://localhost:8000",
  "http://127.0.0.1:8000",
  HARNESS_ORIGIN,
]);


/** 使用固定阶段和公开错误码表达失败，避免携带请求、响应或私密值。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存可公开诊断坐标并生成稳定的无敏感信息错误文本。 */
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 将所有验收断言统一转换为具备稳定阶段和错误码的失败。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** 把任意异常收敛为经过字符约束的公开失败坐标，不透传原始消息。 */
function safeFailure(error, stage = "browser.acceptance") {
  // 只复用可信失败类型或安全坐标，其他异常仅保留类型和当前脚本行号。
  if (error instanceof BrowserAcceptanceFailure) return error;
  if (error?.name === "BrowserAcceptanceFailure"
    && SAFE_COORDINATE.test(String(error.stage ?? ""))
    && SAFE_COORDINATE.test(String(error.code ?? ""))) {
    return new BrowserAcceptanceFailure(error.stage, error.code);
  }
  const name = String(error?.name ?? "error").toLowerCase().replace(/[^a-z0-9_.-]/gu, "");
  const location = String(error?.stack ?? "").match(/prd085-browser-acceptance\.mjs:(\d+):\d+/u);
  return new BrowserAcceptanceFailure(stage, `${name || "error"}_l${location?.[1] ?? "0"}`);
}


/** 校验运行标识字符、长度和路径边界，阻止目录穿越与无界文件名。 */
function safeRunId(value) {
  const candidate = String(value ?? "");
  requireCondition(
    SAFE_RUN_ID.test(candidate) && !candidate.includes("..")
      && !candidate.includes("/") && !candidate.includes("\\"),
    "config.run_id", "invalid",
  );
  return candidate;
}


/** 只接受唯一运行标识参数，拒绝重复、未知或缺少值的命令行输入。 */
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


/** 解析根目录的精确直接子项，并拒绝任何跨层级或特殊目录名称。 */
function directChild(root, name, stage) {
  const target = path.resolve(root, name);
  requireCondition(
    path.dirname(target) === path.resolve(root) && path.basename(target) === name
      && name !== "." && name !== "..",
    stage, "outside_root",
  );
  return target;
}


/** 为本次运行生成不落盘的管理员、角色、停止令牌和目录所有者私密值。 */
function createPrivateConfiguration() {
  /** 按用途前缀生成具备足够熵的私密字符串，避免不同值类别混淆。 */
  const secret = (prefix, bytes = 36) => `${prefix}${randomBytes(bytes).toString("base64url")}`;
  return Object.freeze({
    adminPassword: secret("Prd085-Admin-Aa1-"),
    rolePassword: secret("Prd085-Role-Aa1-"),
    stopToken: secret("Prd085-Stop-"),
    driverOwner: randomBytes(32).toString("hex"),
  });
}


/** 构造最小协调服务环境，只保留必要系统变量并注入本次运行私密配置。 */
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
    PRD085_BROWSER_ADMIN_PASSWORD: privateConfig.adminPassword,
    PRD085_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD085_BROWSER_STOP_TOKEN: privateConfig.stopToken,
  };
}


/** 绑定回环端口确认调试入口未被未知进程占用，失败时立即终止验收。 */
async function assertPortFree(port) {
  const free = await new Promise((resolve) => {
    const server = createServer();
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => server.close(() => resolve(true)));
  });
  requireCondition(free, "config.debug_port", "unavailable");
}


/** 创建带随机所有者标记的浏览器目录，并在部分失败后清理精确子目录。 */
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
    // 创建失败必须保留原始失败语义，同时只清理由本次调用确认创建的目录。
    throw error;
  }
}


/** 仅在路径、类型和所有者标记均匹配时删除本次浏览器运行目录。 */
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
  const entries = await listOperation(directory);
  for (const name of entries) {
    if (name === DRIVER_OWNER_FILENAME) continue;
    // owner marker 必须最后删除；任一锁文件失败时保留它，后续清理仍可验证精确归属。
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


/** 为正式验收构造排除非业务更新器的仓库内 Chrome 硬链接运行树。 */
async function createIsolatedBrowserRuntime(
  runId,
  owner,
  {
    sourceRoot = path.dirname(CHROME),
    mkdirOperation = mkdir,
    listOperation = readdir,
    linkOperation = link,
    writeOperation = writeFile,
    readOperation = readFile,
    removeOperation = rm,
  } = {},
) {
  requireCondition(/^[0-9a-f]{64}$/.test(String(owner)), "config.browser_runtime", "owner");
  await mkdirOperation(BROWSER_RUNTIME_ROOT, { recursive: true });
  const directory = directChild(BROWSER_RUNTIME_ROOT, runId, "config.browser_runtime");
  requireCondition(!existsSync(directory), "config.browser_runtime", "exists");
  let created = false;
  try {
    await mkdirOperation(directory);
    created = true;
    /** 目录只接受真实文件和子目录，拒绝链接并排除会逃逸为独立进程的更新器。 */
    const mirror = async (source, target) => {
      const entries = await listOperation(source, { withFileTypes: true });
      for (const entry of entries) {
        requireCondition(!entry.isSymbolicLink(), "config.browser_runtime", "symlink");
        if (entry.isFile() && EXCLUDED_BROWSER_RUNTIME_FILES.has(entry.name.toLowerCase())) continue;
        const sourcePath = path.join(source, entry.name);
        const targetPath = path.join(target, entry.name);
        if (entry.isDirectory()) {
          await mkdirOperation(targetPath);
          await mirror(sourcePath, targetPath);
        } else {
          requireCondition(entry.isFile(), "config.browser_runtime", "type");
          await linkOperation(sourcePath, targetPath);
        }
      }
    };
    await mirror(sourceRoot, directory);
    const marker = path.join(directory, BROWSER_RUNTIME_OWNER_FILENAME);
    await writeOperation(marker, `${owner}\n`, "ascii");
    requireCondition(
      (await readOperation(marker, "ascii")).trim() === owner
        && existsSync(path.join(directory, "chrome.exe"))
        && !existsSync(path.join(directory, "debug.log")),
      "config.browser_runtime", "layout",
    );
    const updaterEntries = [];
    /** 递归确认任何版本目录都没有带入更新器，避免名称布局变化绕过顶层排除。 */
    const findUpdater = async (current) => {
      for (const entry of await listOperation(current, { withFileTypes: true })) {
        if (entry.name.toLowerCase() === "os_update_handler.exe") updaterEntries.push(entry.name);
        if (entry.isDirectory()) await findUpdater(path.join(current, entry.name));
      }
    };
    await findUpdater(directory);
    requireCondition(updaterEntries.length === 0, "config.browser_runtime", "updater");
    return directory;
  } catch (error) {
    if (created) await removeOperation(directory, { recursive: true, force: false });
    // 构造失败只删除本次直接子目录，并保留原始固定失败坐标。
    throw error;
  }
}


/** 仅在运行树路径、类型和随机所有者均匹配时删除隔离 Chrome 硬链接。 */
async function removeIsolatedBrowserRuntime(
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
  requireCondition(
    path.dirname(path.resolve(directory)) === path.resolve(BROWSER_RUNTIME_ROOT),
    "cleanup.browser_runtime", "boundary",
  );
  const information = await lstatOperation(directory);
  requireCondition(
    information.isDirectory() && !information.isSymbolicLink(),
    "cleanup.browser_runtime", "type",
  );
  const marker = path.join(directory, BROWSER_RUNTIME_OWNER_FILENAME);
  requireCondition(
    (await readOperation(marker, "ascii")).trim() === owner,
    "cleanup.browser_runtime", "owner",
  );
  for (const name of await listOperation(directory)) {
    if (name === BROWSER_RUNTIME_OWNER_FILENAME) continue;
    // owner marker 必须最后删除，确保锁文件失败后仍可安全重试同一运行树。
    await removeOperation(directChild(directory, name, "cleanup.browser_runtime"), {
      recursive: true,
      force: false,
      maxRetries: 10,
      retryDelay: 200,
    });
  }
  await unlinkOperation(marker);
  await rmdirOperation(directory);
}


/** 初始化网络、状态、隐私和暂停响应计数器，且只在内存保存私密值。 */
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
    recordedResponseIds: new Set(),
    loginStatuses: [],
    logoutStatuses: [],
    funnelStatuses: [],
    funnelGetCount: 0,
    noStoreResponses: true,
    noStoreObserved: 0,
    requestContractExact: true,
    pauseExpectation: null,
    pausedResponse: null,
  };
}


/** 扫描子进程输出中的私密探针，只累计命中数而不保存命中内容。 */
function inspectProcessChunk(chunk, tracker) {
  const output = String(chunk ?? "");
  const hits = tracker.privateValues.filter((value) => value && output.includes(value));
  if (hits.length) {
    tracker.processPrivacyViolations += hits.length;
    tracker.processPrivacyViolationCount += 1;
  }
}


/** 启动仓库内 Python 协调服务，等待固定就绪事实并捕获无敏感诊断坐标。 */
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
        /PRD-085 browser harness failed: ([a-z][a-z0-9_.-]*):([a-z][a-z0-9_.-]*)/u,
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


/** 使用仅在内存持有的协调令牌向固定回环服务发送受保护命令。 */
async function postHarness(pathname, privateConfig, body = {}) {
  return fetch(`${HARNESS_ORIGIN}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Harness-Token": privateConfig.stopToken },
    body: JSON.stringify(body),
    redirect: "error",
  });
}


/** 在限定时间内等待协调服务成功退出，并把失败文本解析为安全坐标。 */
async function waitForHarnessClose(harness, timeoutMs = 180_000) {
  let timer;
  const result = await Promise.race([
    harness.closed,
    new Promise((resolve) => { timer = setTimeout(() => resolve(null), timeoutMs); }),
  ]);
  clearTimeout(timer);
  if (!result || result.code !== 0) {
    const coordinate = String(result?.errorOutput ?? "").match(
      /PRD-085 browser harness failed: ([a-z][a-z0-9_.-]*):([a-z][a-z0-9_.-]*)/u,
    );
    throw new BrowserAcceptanceFailure(
      coordinate?.[1] ?? "harness.close",
      coordinate?.[2] ?? "status",
    );
  }
}


/** 请求仍存活的协调服务自行中止；所有失败只返回布尔坐标，不覆盖原始验收异常。 */
async function abortHarness(
  harness,
  privateConfig,
  {
    postOperation = postHarness,
    terminateOperation = terminateProcessTree,
    timeoutMs = 10_000,
  } = {},
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
    try {
      await terminateOperation(harness.child);
    // Windows 终止或 drain 失败只形成固定清理结果，主异常仍由调用方保留。
    } catch {
      return false;
    }
  }
  return true;
}


/** 通过具备所有者校验的基础实现幂等关闭浏览器连接和自有进程。 */
async function closeBrowser(browserClient, pageSession, child) {
  // 关闭操作只作用于已验证的浏览器所有者，不能终止机器上的其他进程。
  return closeOwnedBrowser(browserClient, pageSession, child, { terminateOperation: terminateIdempotentBrowserOwner });
}


/** 判断浏览器请求是否留在允许来源且不携带禁止查询参数或身份信息。 */
function browserRequestAllowed(value) {
  let target;
  try { target = new URL(String(value)); } catch {
    return Object.freeze({ allowed: false, forbiddenQuery: false, external: true });
  }
  if (["data:", "blob:", "about:"].includes(target.protocol)) {
    return Object.freeze({ allowed: true, forbiddenQuery: false, external: false });
  }
  const external = !ALLOWED_ORIGINS.has(target.origin);
  const funnelQuery = target.pathname === "/product-analytics/funnel" && target.search !== "";
  const forbiddenQuery = funnelQuery
    || [...target.searchParams.keys()].some((name) => FORBIDDEN_QUERY_KEY.test(name));
  return Object.freeze({
    allowed: !external && !forbiddenQuery && !target.username && !target.password,
    forbiddenQuery,
    external,
  });
}


/** 按来源、路径和方法分类业务请求，供精确网络合同与状态序列统计。 */
function classifyRequest(value, method = "GET") {
  let target;
  try { target = new URL(String(value)); } catch { return "external"; }
  if (["data:", "blob:", "about:"].includes(target.protocol)) return "internal";
  if (!ALLOWED_ORIGINS.has(target.origin)) return "external";
  const verb = String(method).toUpperCase();
  if (["http://localhost:5173", "http://127.0.0.1:5173"].includes(target.origin)) return "console_asset";
  if (target.origin === HARNESS_ORIGIN) return "harness";
  // 登录、注销和当前身份请求分别计数，以证明会话生命周期完整且后端生效。
  if (target.pathname === "/auth/login" && verb === "POST") return "auth_login";
  if (target.pathname === "/auth/logout" && verb === "POST") return "auth_logout";
  if (target.pathname === "/auth/me" && verb === "GET") return "auth_me";
  if (target.pathname === "/product-analytics/funnel" && verb === "GET") return "product_analytics_funnel";
  if (target.pathname === "/console/navigation" && verb === "GET") return "console_navigation";
  return "control_local";
}


/** 兼容数组和对象两种协议头表示，以大小写无关方式读取单个头值。 */
function headerValue(headers, name) {
  if (Array.isArray(headers)) {
    const entry = headers.find((item) => String(item?.name ?? "").toLowerCase() === String(name).toLowerCase());
    return entry ? String(entry.value ?? "") : "";
  }
  const entry = Object.entries(headers ?? {}).find(([key]) => key.toLowerCase() === String(name).toLowerCase());
  return entry ? String(entry[1]) : "";
}


/** 登记浏览器会话令牌并验证漏斗请求的方法、路径、头和无正文合同。 */
function inspectBusinessRequest(tracker, request) {
  const category = classifyRequest(request?.url, request?.method);
  const authorization = headerValue(request?.headers, "Authorization");
  const browserToken = authorization.match(/^Bearer ([^\s]+)$/)?.[1] ?? "";
  // 捕获的会话令牌只进入内存隐私扫描集合，不得写入网络证据或进程输出。
  if (browserToken) {
    tracker.browserTokens.add(browserToken);
    // 去重后登记令牌，确保后续 DOM、日志和产物扫描覆盖每次登录会话。
    if (!tracker.privateValues.includes(browserToken)) tracker.privateValues.push(browserToken);
  }
  if (category !== "product_analytics_funnel") return;
  let target;
  try { target = new URL(String(request.url)); } catch {
    tracker.requestContractExact = false;
    return;
  }
  const exact = String(request.method).toUpperCase() === "GET"
    && target.pathname === "/product-analytics/funnel"
    && target.search === ""
    && target.hash === ""
    && !target.username
    && !target.password
    && request.hasPostData !== true
    && !Object.hasOwn(request, "postData")
    && headerValue(request.headers, "Accept").toLowerCase() === "application/json"
    && /^Bearer [^\s]+$/.test(headerValue(request.headers, "Authorization"))
    && headerValue(request.headers, "Content-Type") === "";
  tracker.requestContractExact &&= exact;
  tracker.funnelGetCount += 1;
}


/** 按请求分类记录公开状态码，并检查漏斗响应始终声明禁止缓存。 */
function recordResponseStatus(tracker, response) {
  const requestId = String(response?.requestId ?? "");
  if (requestId && tracker.recordedResponseIds.has(requestId)) return;
  const method = tracker.requestMethods.get(requestId) ?? String(response?.method ?? "GET");
  const category = tracker.requestCategories.get(requestId) ?? classifyRequest(response?.url, method);
  const status = Number(response?.status ?? 0);
  if (requestId) tracker.recordedResponseIds.add(requestId);
  // 会话入口和出口状态分别保存，避免仅凭页面变化推断后端鉴权成功。
  if (category === "auth_login") tracker.loginStatuses.push(status);
  if (category === "auth_logout") tracker.logoutStatuses.push(status);
  if (category === "product_analytics_funnel") {
    tracker.funnelStatuses.push(status);
    tracker.noStoreObserved += 1;
    tracker.noStoreResponses &&= headerValue(response?.headers, "Cache-Control")
      .toLowerCase().startsWith("no-store");
  }
}


/** 等待并暂停下一条指定响应，用于验证撤权后迟到数据不会重新渲染。 */
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


/** 释放已暂停响应；仅在明确允许页面关闭时接受协议会话已经结束。 */
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


/** 绑定网络、控制台和页面异常守卫，拦截外部请求及 URL 中的私密值。 */
async function bindBrowserGuards(client, sessionId, tracker) {
  client.on("Network.requestWillBeSent", (event, eventSessionId) => {
    // 只处理当前页面会话且具备有效地址的请求，避免跨会话污染合同计数。
    if (eventSessionId !== sessionId || !event?.request?.url) return;
    const requestId = String(event.requestId ?? "");
    const method = String(event.request.method ?? "GET");
    tracker.requestMethods.set(requestId, method);
    tracker.requestCategories.set(requestId, classifyRequest(event.request.url, method));
  });
  client.on("Network.responseReceived", (event, eventSessionId) => {
    // 响应状态只按当前页面会话登记，不读取或保存任何响应正文。
    if (eventSessionId === sessionId) recordResponseStatus(tracker, {
      requestId: event?.requestId,
      url: event?.response?.url,
      status: event?.response?.status,
      headers: event?.response?.headers,
    });
  });
  client.on("Runtime.consoleAPICalled", (event, eventSessionId) => {
    // 当前页面的错误级控制台消息只累计数量，禁止保存可能含私密值的文本。
    if (eventSessionId === sessionId && ["error", "assert"].includes(String(event?.type))) tracker.consoleErrors += 1;
  });
  client.on("Runtime.exceptionThrown", (_event, eventSessionId) => {
    // 页面异常同样只累计数量，避免把堆栈或请求数据带入正式证据。
    if (eventSessionId === sessionId) tracker.pageErrors += 1;
  });
  client.on("Fetch.requestPaused", (event, eventSessionId) => {
    // 仅处理当前会话中具备请求标识和地址的暂停事件，其他事件立即忽略。
    if (eventSessionId !== sessionId || !event?.requestId || !event?.request?.url) return;
    if (Number.isFinite(Number(event.responseStatusCode))) {
      const category = classifyRequest(event.request.url, event.request.method);
      const status = Number(event.responseStatusCode);
      recordResponseStatus(tracker, {
        requestId: event.networkId ?? event.requestId,
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
    inspectBusinessRequest(tracker, event.request);
    const guard = browserRequestAllowed(event.request.url);
    if (guard.external) tracker.externalRequests += 1;
    if (guard.forbiddenQuery) tracker.forbiddenQueries += 1;
    const suppliedPrivate = tracker.privateValues.some(
      (item) => item && String(event.request.url).includes(item),
    );
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
    { urlPattern: `${CONTROL_ORIGIN}/product-analytics/funnel`, requestStage: "Response" },
    { urlPattern: "http://127.0.0.1:8001/product-analytics/funnel", requestStage: "Response" },
  ] }, sessionId);
}


/** 使用统一长超时等待真实页面条件，保持所有 DOM 门禁的失败坐标一致。 */
async function waitForDom(client, sessionId, expression, stage) {
  // DOM 等待只返回公开布尔结果，不读取令牌、请求正文或上游响应内容。
  return waitForDomBase(client, sessionId, expression, stage, DOM_WAIT_TIMEOUT_MS);
}


/** 等待连续两帧渲染完成，避免在布局仍变化时生成验收截图。 */
async function settle(client, sessionId, stage) {
  return evaluate(
    client,
    sessionId,
    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))",
    stage,
  );
}


/** 导航到本地控制台目标页并等待真实登录表单进入可操作状态。 */
async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/#growth-analytics` }, sessionId),
    "browser.console.load",
    60_000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login");
}


/** 通过真实表单登录指定租户，并等待后端成功状态及已登录页面状态。 */
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
  await waitForDom(client, sessionId, "document.querySelector('#logout-button')", `${stage}.dom`);
  await waitForTrackerCount(() => tracker.loginStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.loginStatuses.at(-1) === 200, stage, "status");
}


/** 点击真实注销控件并确认登录表单恢复；失效 Session 场景允许统一 401。 */
async function logout(client, sessionId, tracker, stage, expectedStatus = 200) {
  const before = tracker.logoutStatuses.length;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('#logout-button');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `${stage}.form`);
  await waitForTrackerCount(() => tracker.logoutStatuses.length, before + 1, `${stage}.network`, 60_000);
  requireCondition(tracker.logoutStatuses.at(-1) === expectedStatus, stage, "status");
}


/** 打开增长分析页面并等待七阶段漏斗完成加载且没有错误占位。 */
async function openAnalyticsPage(client, sessionId, stage) {
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('.growth-analytics-panel') || document.querySelector('[data-console-nav=\"growth-analytics\"]')",
    `${stage}.shell`,
  );
  const ready = await evaluate(
    client,
    sessionId,
    "Boolean(document.querySelector('.growth-analytics-panel .product-analytics-summary'))",
    `${stage}.current`,
  );
  if (!ready) {
    const clicked = await evaluate(client, sessionId, `(() => {
      const button=document.querySelector('[data-console-nav="growth-analytics"]');
      if (!button || button.disabled) return false; button.click(); return true;
    })()`, `${stage}.navigate`);
    requireCondition(clicked === true, stage, "navigation");
  }
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('.growth-analytics-panel .product-analytics-summary')"
      + " && document.querySelectorAll('.growth-analytics-panel .product-analytics-step').length===7"
      + " && !document.querySelector('.growth-analytics-panel .product-analytics-loading,.growth-analytics-panel .product-analytics-error')",
    `${stage}.ready`,
  );
}


/** 从 DOM 提取固定布尔和计数事实，不采集租户名称或业务响应正文。 */
async function readAnalyticsSnapshot(client, sessionId, stage) {
  // 页面快照只返回阶段数量、完成数量和显示状态，不返回租户或请求标识。
  return evaluate(client, sessionId, `(() => {
    const root=document.querySelector('.growth-analytics-panel');
    const steps=[...(root?.querySelectorAll('.product-analytics-step')||[])];
    const rows=[...(root?.querySelectorAll('.product-analytics-row:not(.product-analytics-table-head)')||[])];
    const metrics=[...(root?.querySelectorAll('.product-analytics-metrics > div')||[])];
    const text=root?.textContent||'';
    return {
      root:Boolean(root), steps:steps.length, rows:rows.length, metrics:metrics.length,
      orders:steps.map((item)=>item.getAttribute('data-stage-order')).join(','),
      completed:steps.filter((item)=>item.classList.contains('is-complete')).length,
      initial:Boolean(root?.querySelector('.product-analytics-initial')),
      rate:text.includes('%'), count:metrics.some((item)=>/\\d/.test(item.textContent||'')),
      noError:!root?.querySelector('.product-analytics-error,.product-analytics-loading'),
    };
  })()`, stage);
}


/** 计算可见性、溢出、重叠和内容边界，覆盖桌面及移动视口布局门禁。 */
async function readLayoutMetrics(client, sessionId, focusSelector) {
  // 布局读取只提取尺寸和布尔指标，不采集 DOM 文本或任何会话信息。
  return evaluate(client, sessionId, `(() => {
    const root=document.querySelector('.growth-analytics-panel');
    const focus=document.querySelector(${JSON.stringify(focusSelector)});
    /** 判断节点是否实际占据可见布局空间，供重叠与聚焦检查共同使用。 */
    const shown=(item)=>{ if(!item)return false; const style=getComputedStyle(item); const rect=item.getBoundingClientRect();
      return style.display!=='none' && style.visibility!=='hidden' && rect.width>0 && rect.height>0; };
    const controls=[...(root?.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])')||[])].filter(shown);
    let overlaps=0;
    for(let first=0;first<controls.length;first+=1){ const a=controls[first].getBoundingClientRect();
      for(let second=first+1;second<controls.length;second+=1){
        if(controls[first].contains(controls[second])||controls[second].contains(controls[first]))continue;
        const b=controls[second].getBoundingClientRect();
        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>2 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>2)overlaps+=1;
      }}
    const textOverflow=[...(root?.querySelectorAll('button,label,span,strong,small,time,p,h2,h3')||[])]
      .filter((item)=>shown(item) && !item.closest('.product-analytics-table'))
      .some((item)=>!['auto','scroll'].includes(getComputedStyle(item).overflowX) && item.scrollWidth>item.clientWidth+2);
    const rootRect=root?.getBoundingClientRect();
    const focusRect=focus?.getBoundingClientRect();
    return {
      width:innerWidth,height:innerHeight,rootVisible:shown(root),focusVisible:shown(focus)
        && focusRect.bottom>0 && focusRect.top<innerHeight && focusRect.right>0 && focusRect.left<innerWidth,
      horizontalOverflow:document.documentElement.scrollWidth>innerWidth+1 || document.body.scrollWidth>innerWidth+1,
      textOverflow,overlapCount:overlaps,
      contentBounded:Boolean(rootRect) && rootRect.left>=-1 && rootRect.right<=innerWidth+1,
    };
  })()`, "browser.layout.read");
}


/** 截图前验证 DOM 隐私和布局，临时遮蔽用户区后校验像素并写入白名单文件。 */
async function captureScreenshot(client, sessionId, staging, filename, viewport, focusSelector, tracker, stage) {
  await setViewport(client, sessionId, viewport);
  requireCondition(await evaluate(client, sessionId, `(() => {
    const focus=document.querySelector(${JSON.stringify(focusSelector)});
    if(!focus)return false; focus.scrollIntoView({block:'center',inline:'nearest'}); return true;
  })()`, `${stage}.focus`) === true, stage, "focus");
  await settle(client, sessionId, `${stage}.settle`);
  const privacy = await evaluate(client, sessionId, `(() => {
    const needles=${JSON.stringify(tracker.privateValues)};
    const root=document.querySelector('.growth-analytics-panel');
    const values=[root?.textContent||'',...[...(root?.querySelectorAll('input,textarea,select')||[])].map((item)=>String(item.value||'')),
      ...[...(root?.querySelectorAll('*')||[])].flatMap((item)=>[...item.attributes].map((attribute)=>attribute.value))];
    return needles.every((needle)=>!values.some((value)=>value.includes(needle)));
  })()`, `${stage}.privacy`);
  requireCondition(privacy === true, stage, "privacy");
  const layout = await readLayoutMetrics(client, sessionId, focusSelector);
  requireCondition(layout.width === viewport.width && layout.height === viewport.height
    && layout.rootVisible && layout.focusVisible && !layout.horizontalOverflow
    && !layout.textOverflow && layout.overlapCount === 0 && layout.contentBounded,
  stage, "layout");
  await evaluate(client, sessionId, `(() => { const item=document.querySelector('.user-menu'); if(!item)return false;
    item.dataset.prd085Mask='true'; item.style.visibility='hidden'; return true; })()`, `${stage}.mask`);
  let result;
  try {
    result = await client.send("Page.captureScreenshot", {
      format: "png", fromSurface: true, captureBeyondViewport: false,
    }, sessionId, 30_000);
  } finally {
    await evaluate(client, sessionId, `(() => { const item=document.querySelector('[data-prd085-mask=true]'); if(!item)return false;
      item.style.visibility=''; delete item.dataset.prd085Mask; return true; })()`, `${stage}.restore`);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  inspectPngPixels(buffer, viewport, stage);
  await writeFile(path.join(staging, filename), buffer);
  return Object.freeze({ layout, nonblank: true, noSensitiveDom: true });
}


/** 验证授权访问、响应暂停、动态撤权清屏和迟到响应丢弃的完整生命周期。 */
async function exerciseAuthorizedAndRevocation(client, sessionId, tracker, staging, privateConfig) {
  const before = tracker.funnelGetCount;
  await login(client, sessionId, tracker, "default", OPS_EMAIL, privateConfig.rolePassword, "browser.login.authorized");
  const navigationVisible = await evaluate(
    client, sessionId,
    "Boolean(document.querySelector('[data-console-nav=\"growth-analytics\"]'))",
    "browser.authorized.navigation",
  );
  await openAnalyticsPage(client, sessionId, "browser.authorized.analytics");
  await waitForTrackerCount(() => tracker.funnelGetCount, before + 1, "browser.authorized.network", 60_000);
  const snapshot = await readAnalyticsSnapshot(client, sessionId, "browser.authorized.snapshot");
  requireCondition(navigationVisible === true, "browser.authorized", "navigation");
  requireCondition(snapshot.root === true, "browser.authorized", "root");
  requireCondition(snapshot.steps === 7, "browser.authorized", "steps");
  requireCondition(snapshot.rows === 7, "browser.authorized", "rows");
  requireCondition(snapshot.metrics === 4, "browser.authorized", "metrics");
  requireCondition(snapshot.orders === "1,2,3,4,5,6,7", "browser.authorized", "orders");
  requireCondition(snapshot.completed === 2, "browser.authorized", "completed");
  requireCondition(snapshot.rate === true, "browser.authorized", "rate");
  requireCondition(snapshot.count === true, "browser.authorized", "count");
  requireCondition(snapshot.noError === true, "browser.authorized", "error_state");
  const desktop = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[0], VIEWPORTS.desktop,
    ".product-analytics-summary", tracker, "browser.screenshot.authorized_desktop",
  );
  const mobile = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[1], VIEWPORTS.mobile,
    ".product-analytics-steps", tracker, "browser.screenshot.authorized_mobile",
  );

  const paused = pauseNextResponse(
    tracker, "product_analytics_funnel", 200, "browser.revoke.pause",
  );
  const refreshed = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-product-analytics-refresh]');
    if(!button||button.disabled)return false; button.click(); return true;
  })()`, "browser.revoke.refresh");
  requireCondition(refreshed === true, "browser.revoke.refresh", "button");
  await paused;
  requireCondition(
    (await postHarness("/revoke-product-analytics-read", privateConfig, {})).status === 200,
    "browser.revoke", "status",
  );
  const navigated = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-console-nav=overview]');
    if(!button||button.disabled)return false; button.click(); return true;
  })()`, "browser.revoke.navigate");
  requireCondition(navigated === true, "browser.revoke.navigate", "button");
  await waitForDom(
    client,
    sessionId,
    "!document.querySelector('[data-console-nav=\"growth-analytics\"]')"
      + " && !document.querySelector('.growth-analytics-panel .product-analytics-summary,.growth-analytics-panel .product-analytics-step')",
    "browser.revoke.cleared",
  );
  const beforeRelease = await evaluate(client, sessionId, `(() => ({
    root:!document.querySelector('.growth-analytics-panel') || document.querySelector('.growth-analytics-panel').childElementCount===0,
    navigation:!document.querySelector('[data-console-nav="growth-analytics"]'),
    dom:!document.querySelector('.product-analytics-summary,.product-analytics-step,.product-analytics-row'),
  }))()`, "browser.revoke.before_release");
  await releasePausedResponse(client, tracker, "browser.revoke.release", { allowClosed: true });
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const lateDiscarded = await evaluate(client, sessionId, `(() =>
    !document.querySelector('.product-analytics-summary,.product-analytics-step,.product-analytics-row')
      && !document.querySelector('[data-console-nav="growth-analytics"]'))()`, "browser.revoke.late");
  await logout(client, sessionId, tracker, "browser.logout.authorized");

  requireCondition(
    (await postHarness("/restore-product-analytics-read", privateConfig, {})).status === 200,
    "browser.permission_denied.restore", "status",
  );
  const beforePermissionDenied = tracker.funnelGetCount;
  await login(
    client, sessionId, tracker, "default", OPS_EMAIL, privateConfig.rolePassword,
    "browser.login.permission_denied",
  );
  await openAnalyticsPage(client, sessionId, "browser.permission_denied.analytics");
  await waitForTrackerCount(
    () => tracker.funnelGetCount,
    beforePermissionDenied + 1,
    "browser.permission_denied.success_network",
    60_000,
  );
  requireCondition(
    (await postHarness("/revoke-product-analytics-read", privateConfig, {})).status === 200,
    "browser.permission_denied.revoke", "status",
  );
  const beforeDeniedStatus = tracker.funnelStatuses.length;
  const deniedRefresh = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-product-analytics-refresh]');
    if(!button||button.disabled)return false; button.click(); return true;
  })()`, "browser.permission_denied.refresh");
  requireCondition(deniedRefresh === true, "browser.permission_denied.refresh", "button");
  await waitForTrackerCount(
    () => tracker.funnelStatuses.length,
    beforeDeniedStatus + 1,
    "browser.permission_denied.network",
    60_000,
  );
  requireCondition(tracker.funnelStatuses.at(-1) === 403, "browser.permission_denied", "status");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('#logout-button')"
      + " && !document.querySelector('[data-console-nav=\"growth-analytics\"]')"
      + " && !document.querySelector('.product-analytics-summary,.product-analytics-step,.product-analytics-row')",
    "browser.permission_denied.cleared",
  );
  const permissionDeniedCleared = await evaluate(client, sessionId, `(() =>
    Boolean(document.querySelector('#logout-button'))
      && !document.querySelector('[data-console-nav="growth-analytics"]')
      && !document.querySelector('.product-analytics-summary,.product-analytics-step,.product-analytics-row'))()`,
  "browser.permission_denied.state");
  await logout(client, sessionId, tracker, "browser.logout.permission_denied");
  requireCondition(
    (await postHarness("/restore-product-analytics-read", privateConfig, {})).status === 200,
    "browser.authentication.restore", "status",
  );
  return Object.freeze({
    navigationVisible,
    pageVisible: snapshot.root,
    desktop,
    mobile,
    snapshot,
    root: beforeRelease.root,
    navigation: beforeRelease.navigation,
    dom: beforeRelease.dom,
    lateDiscarded,
    permissionDeniedCleared,
  });
}


/** 真实触发当前 401 与迟到 401，并在旧失败释放前建立第二租户的新身份。 */
async function exerciseAuthenticationFailures(client, sessionId, tracker, privateConfig) {
  let before = tracker.funnelGetCount;
  await login(
    client, sessionId, tracker, "default", OPS_EMAIL, privateConfig.rolePassword,
    "browser.login.session_expired",
  );
  await openAnalyticsPage(client, sessionId, "browser.session_expired.analytics");
  await waitForTrackerCount(
    () => tracker.funnelGetCount, before + 1, "browser.session_expired.success_network", 60_000,
  );
  requireCondition(
    (await postHarness("/invalidate-authorized-sessions", privateConfig, {})).status === 200,
    "browser.session_expired.invalidate", "status",
  );
  const beforeExpiredStatus = tracker.funnelStatuses.length;
  const expiredRefresh = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-product-analytics-refresh]');
    if(!button||button.disabled)return false; button.click(); return true;
  })()`, "browser.session_expired.refresh");
  requireCondition(expiredRefresh === true, "browser.session_expired.refresh", "button");
  await waitForTrackerCount(
    () => tracker.funnelStatuses.length,
    beforeExpiredStatus + 1,
    "browser.session_expired.network",
    60_000,
  );
  requireCondition(tracker.funnelStatuses.at(-1) === 401, "browser.session_expired", "status");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('#login-form')"
      + " && !document.querySelector('#logout-button')"
      + " && !document.querySelector('.product-analytics-summary,.product-analytics-step,.product-analytics-row')",
    "browser.session_expired.cleared",
  );
  const sessionExpiredCleared = await evaluate(client, sessionId, `(() =>
    Boolean(document.querySelector('#login-form')) && !document.querySelector('#logout-button')
      && !document.querySelector('.product-analytics-summary,.product-analytics-step,.product-analytics-row'))()`,
  "browser.session_expired.state");

  before = tracker.funnelGetCount;
  await login(
    client, sessionId, tracker, "default", OPS_EMAIL, privateConfig.rolePassword,
    "browser.login.late_auth",
  );
  await openAnalyticsPage(client, sessionId, "browser.late_auth.analytics");
  await waitForTrackerCount(
    () => tracker.funnelGetCount, before + 1, "browser.late_auth.success_network", 60_000,
  );
  const paused = pauseNextResponse(
    tracker, "product_analytics_funnel", 401, "browser.late_auth.pause",
  );
  requireCondition(
    (await postHarness("/invalidate-authorized-sessions", privateConfig, {})).status === 200,
    "browser.late_auth.invalidate", "status",
  );
  const lateRefresh = await evaluate(client, sessionId, `(() => {
    const button=document.querySelector('[data-product-analytics-refresh]');
    if(!button||button.disabled)return false; button.click(); return true;
  })()`, "browser.late_auth.refresh");
  requireCondition(lateRefresh === true, "browser.late_auth.refresh", "button");
  await paused;
  requireCondition(
    (await postHarness("/revoke-product-analytics-read", privateConfig, {})).status === 200,
    "browser.late_auth.final_revoke", "status",
  );
  await logout(client, sessionId, tracker, "browser.logout.late_auth", 401);
  const secondaryBefore = tracker.funnelGetCount;
  await login(
    client, sessionId, tracker, "prd085-secondary", SECONDARY_EMAIL, privateConfig.adminPassword,
    "browser.login.secondary",
  );
  await releasePausedResponse(client, tracker, "browser.late_auth.release", { allowClosed: true });
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const lateAuthFailureDiscarded = await evaluate(client, sessionId, `(() =>
    Boolean(document.querySelector('#logout-button')) && !document.querySelector('#login-form'))()`,
  "browser.late_auth.discarded");
  // 只返回固定布尔和计数坐标，不能把新旧租户身份或 Session 带入正式证据。
  return Object.freeze({ sessionExpiredCleared, lateAuthFailureDiscarded, secondaryBefore });
}


/** 验证无权限角色看不到导航和页面，并且前端不会预取受保护接口。 */
async function exerciseUnauthorized(client, sessionId, tracker, privateConfig) {
  const before = tracker.funnelGetCount;
  await login(client, sessionId, tracker, "default", DEVELOPER_EMAIL, privateConfig.rolePassword, "browser.login.unauthorized");
  await settle(client, sessionId, "browser.unauthorized.settle");
  await new Promise((resolve) => { setTimeout(resolve, 300); });
  const state = await evaluate(client, sessionId, `(() => ({
    navigation:!document.querySelector('[data-console-nav="growth-analytics"]'),
    page:!document.querySelector('.growth-analytics-panel,.product-analytics-summary,.product-analytics-step'),
  }))()`, "browser.unauthorized.state");
  const apiPrefetchAbsent = tracker.funnelGetCount === before;
  requireCondition(state.navigation && state.page && apiPrefetchAbsent, "browser.unauthorized", "boundary");
  await logout(client, sessionId, tracker, "browser.logout.unauthorized");
  return Object.freeze({ ...state, apiPrefetchAbsent });
}


/** 验证第二租户拥有独立漏斗数据，并在注销后清空浏览器持久状态。 */
async function exerciseSecondary(
  client, sessionId, tracker, staging, privateConfig,
  { alreadyLoggedIn = false, before = tracker.funnelGetCount } = {},
) {
  if (!alreadyLoggedIn) {
    await login(
      client, sessionId, tracker, "prd085-secondary", SECONDARY_EMAIL, privateConfig.adminPassword,
      "browser.login.secondary",
    );
  }
  const navigationVisible = await evaluate(
    client, sessionId,
    "Boolean(document.querySelector('[data-console-nav=\"growth-analytics\"]'))",
    "browser.secondary.navigation",
  );
  await openAnalyticsPage(client, sessionId, "browser.secondary.analytics");
  await waitForTrackerCount(() => tracker.funnelGetCount, before + 1, "browser.secondary.network", 60_000);
  const snapshot = await readAnalyticsSnapshot(client, sessionId, "browser.secondary.snapshot");
  const independent = snapshot.root && snapshot.steps === 7 && snapshot.rows === 7
    && snapshot.completed === 1 && snapshot.initial && snapshot.noError;
  requireCondition(navigationVisible && independent, "browser.secondary", "isolation");
  const desktop = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[2], VIEWPORTS.desktop,
    ".product-analytics-initial", tracker, "browser.screenshot.secondary_desktop",
  );
  const mobile = await captureScreenshot(
    client, sessionId, staging, SCREENSHOTS[3], VIEWPORTS.mobile,
    ".product-analytics-initial", tracker, "browser.screenshot.secondary_mobile",
  );
  await logout(client, sessionId, tracker, "browser.logout.secondary");
  const storageCleared = await evaluate(
    client, sessionId,
    "localStorage.length===0 && sessionStorage.length===0",
    "browser.logout.storage",
  );
  return Object.freeze({
    navigationVisible,
    independent,
    previous: snapshot.completed === 1 && snapshot.initial,
    desktop,
    mobile,
    snapshot,
    storageCleared,
  });
}


/** 验证最终页面、表单、地址、缓存和浏览器数据库均未残留私密值。 */
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
  requireCondition(
    result.hits === 0 && result.caches === 0 && result.databases === 0 && result.login,
    "browser.privacy", "persistent",
  );
  return true;
}


/** 确认漏斗请求次数、成功状态序列和无正文请求合同全部精确匹配。 */
function requestContractsExact(tracker) {
  return tracker.requestContractExact
    && tracker.funnelGetCount === 9
    && JSON.stringify(tracker.funnelStatuses)
      === JSON.stringify([200, 200, 200, 403, 200, 401, 200, 401, 200]);
}


/** 将浏览器观察结果归一化为固定布尔、计数和状态数组，不包含业务私密字段。 */
function buildCompletionFacts({
  authorized, authentication, unauthorized, secondary, registration, tracker,
}) {
  return {
    status: "passed",
    desktop: {
      width: 1280,
      height: 720,
      nonblank: authorized.desktop.nonblank && secondary.desktop.nonblank,
      page_117: authorized.pageVisible,
      seven_stages: authorized.snapshot.steps === 7 && secondary.snapshot.steps === 7,
      backend_counts_visible: authorized.snapshot.count && secondary.snapshot.count,
      backend_rates_visible: authorized.snapshot.rate && secondary.snapshot.rate,
      initial_state_visible: secondary.snapshot.initial,
      horizontal_overflow_free: authorized.desktop.layout.horizontalOverflow === false
        && secondary.desktop.layout.horizontalOverflow === false,
      content_bounded: authorized.desktop.layout.contentBounded && secondary.desktop.layout.contentBounded,
      text_overflow_free: authorized.desktop.layout.textOverflow === false
        && secondary.desktop.layout.textOverflow === false,
      overlap_count: Math.max(
        authorized.desktop.layout.overlapCount, secondary.desktop.layout.overlapCount,
      ),
      no_sensitive_dom: authorized.desktop.noSensitiveDom && secondary.desktop.noSensitiveDom,
    },
    mobile: {
      width: 390,
      height: 844,
      nonblank: authorized.mobile.nonblank && secondary.mobile.nonblank,
      page_117: authorized.pageVisible,
      seven_stages: authorized.snapshot.steps === 7 && secondary.snapshot.steps === 7,
      backend_counts_visible: authorized.snapshot.count && secondary.snapshot.count,
      backend_rates_visible: authorized.snapshot.rate && secondary.snapshot.rate,
      tenant_isolation_visible: secondary.independent,
      horizontal_overflow_free: authorized.mobile.layout.horizontalOverflow === false
        && secondary.mobile.layout.horizontalOverflow === false,
      content_bounded: authorized.mobile.layout.contentBounded && secondary.mobile.layout.contentBounded,
      text_overflow_free: authorized.mobile.layout.textOverflow === false
        && secondary.mobile.layout.textOverflow === false,
      overlap_count: Math.max(
        authorized.mobile.layout.overlapCount, secondary.mobile.layout.overlapCount,
      ),
      no_sensitive_dom: authorized.mobile.noSensitiveDom && secondary.mobile.noSensitiveDom,
    },
    lifecycle: {
      authorized_navigation_visible: authorized.navigationVisible,
      authorized_page_visible: authorized.pageVisible,
      unauthorized_navigation_absent: unauthorized.navigation,
      unauthorized_page_absent: unauthorized.page,
      unauthorized_api_prefetch_absent: unauthorized.apiPrefetchAbsent,
      backend_permission_denied: true,
      dynamic_revocation_root_cleared: authorized.root,
      dynamic_revocation_navigation_cleared: authorized.navigation,
      dynamic_revocation_dom_cleared: authorized.dom,
      late_response_discarded: authorized.lateDiscarded,
      permission_denied_dom_cleared: authorized.permissionDeniedCleared,
      session_expired_dom_cleared: authentication.sessionExpiredCleared,
      late_auth_failure_discarded: authentication.lateAuthFailureDiscarded,
      secondary_navigation_visible: secondary.navigationVisible,
      secondary_tenant_independent: secondary.independent,
      previous_tenant_dom_cleared:
        authorized.snapshot.completed === 2 && secondary.snapshot.completed === 1,
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
      funnel_get_count: tracker.funnelGetCount,
      funnel_statuses: tracker.funnelStatuses,
      no_store_responses: tracker.noStoreResponses && tracker.noStoreObserved === 9,
      request_contract_exact: requestContractsExact(tracker),
      no_response_body_capture: true,
    },
    screenshots: SCREENSHOTS,
    all_108_prds_complete: false,
  };
}


/** 在提交协调服务前验证桌面、移动、权限生命周期和网络事实完整准确。 */
function validateCompletionFacts(value) {
  requireCondition(
    value?.status === "passed" && value?.all_108_prds_complete === false,
    "browser.complete", "status",
  );
  requireCondition(
    value.desktop?.width === 1280 && value.desktop?.height === 720
      && value.desktop?.overlap_count === 0,
    "browser.desktop", "layout",
  );
  requireCondition(
    value.mobile?.width === 390 && value.mobile?.height === 844
      && value.mobile?.overlap_count === 0,
    "browser.mobile", "layout",
  );
  for (const section of [value.desktop, value.mobile, value.lifecycle]) {
    requireCondition(
      Object.entries(section).every(
        ([name, item]) => ["width", "height", "overlap_count"].includes(name) || item === true,
      ),
      "browser.complete", "facts",
    );
  }
  requireCondition(value.network?.external_requests === 0, "browser.network", "external_requests");
  requireCondition(value.network?.forbidden_queries === 0, "browser.network", "forbidden_queries");
  requireCondition(value.network?.console_errors === 0, "browser.network", "console_errors");
  requireCondition(value.network?.page_errors === 0, "browser.network", "page_errors");
  requireCondition(
    JSON.stringify(value.network?.login_statuses)
      === JSON.stringify([200, 200, 200, 200, 200, 200]),
    "browser.network", "login_statuses",
  );
  requireCondition(
    JSON.stringify(value.network?.logout_statuses) === JSON.stringify([200, 200, 401, 200, 200]),
    "browser.network", "logout_statuses",
  );
  requireCondition(value.network?.funnel_get_count === 9, "browser.network", "funnel_count");
  requireCondition(
    JSON.stringify(value.network?.funnel_statuses)
      === JSON.stringify([200, 200, 200, 403, 200, 401, 200, 401, 200]),
    "browser.network", "funnel_statuses",
  );
  requireCondition(value.network?.no_store_responses === true, "browser.network", "cache_control");
  requireCondition(
    value.network?.request_contract_exact === true,
    "browser.network", "request_contract",
  );
  requireCondition(
    value.network?.no_response_body_capture === true,
    "browser.network", "response_body_capture",
  );
  requireCondition(
    JSON.stringify(value.screenshots) === JSON.stringify(SCREENSHOTS),
    "browser.screenshots", "names",
  );
  return value;
}


/** 协调真实服务和浏览器完成验收，并保证失败路径仍执行所有者安全清理。 */
async function run(runId) {
  requireCondition(existsSync(PYTHON) && existsSync(HARNESS) && existsSync(CHROME), "config.runtime", "missing");
  await assertPortFree(DEBUG_PORT);
  const privateConfig = createPrivateConfiguration();
  const tracker = createTracker(privateConfig);
  let driverDirectory = null;
  let browserRuntimeDirectory = null;
  let staging = null;
  let harness = null;
  let browserChild = null;
  let browserClient = null;
  let pageSession = null;
  let primaryFailure = null;
  try {
    driverDirectory = await createDriverDirectory(runId, privateConfig.driverOwner);
    browserRuntimeDirectory = await createIsolatedBrowserRuntime(
      runId, privateConfig.driverOwner,
    );
    staging = directChild(ARTIFACT_ROOT, `.${runId}.staging`, "config.staging");
    harness = startHarness(runId, privateConfig, driverDirectory, tracker);
    await harness.ready;
    requireCondition(existsSync(staging), "config.staging", "missing");
    browserChild = launchEdge({
      edgePath: path.join(browserRuntimeDirectory, "chrome.exe"),
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
      "Emulation.setTimezoneOverride", { timezoneId: "UTC" }, pageSession.sessionId,
    );
    await pageSession.client.send("Page.addScriptToEvaluateOnNewDocument", { source: `(() => {
      const original=window.setInterval.bind(window); window.setInterval=(callback,delay,...args)=>Number(delay)===15000?0:original(callback,delay,...args);
    })();` }, pageSession.sessionId);
    await loadConsole(pageSession.client, pageSession.sessionId);
    const authorized = await exerciseAuthorizedAndRevocation(
      pageSession.client, pageSession.sessionId, tracker, staging, privateConfig,
    );
    const authentication = await exerciseAuthenticationFailures(
      pageSession.client, pageSession.sessionId, tracker, privateConfig,
    );
    const secondary = await exerciseSecondary(
      pageSession.client, pageSession.sessionId, tracker, staging, privateConfig,
      { alreadyLoggedIn: true, before: authentication.secondaryBefore },
    );
    const unauthorized = await exerciseUnauthorized(
      pageSession.client, pageSession.sessionId, tracker, privateConfig,
    );
    await verifyFinalPrivacy(pageSession.client, pageSession.sessionId, tracker);
    requireCondition(
      tracker.browserTokens.size === 6 && tracker.processPrivacyViolations === 0
        && tracker.processPrivacyViolationCount === 0 && tracker.edgeProcessErrorCount === 0,
      "browser.process", "privacy_or_error",
    );
    const registration = await postHarness("/register-private", privateConfig, {
      values: [...new Set([privateConfig.driverOwner, ...tracker.browserTokens])],
    });
    requireCondition(registration.status === 200, "harness.private", "status");
    const facts = buildCompletionFacts({
      authorized, authentication, unauthorized, secondary, registration, tracker,
    });
    validateCompletionFacts(facts);
    await closeBrowser(browserClient, pageSession, browserChild);
    browserClient = null;
    pageSession = null;
    browserChild = null;
    await removeDriverDirectory(driverDirectory, privateConfig.driverOwner);
    await removeIsolatedBrowserRuntime(browserRuntimeDirectory, privateConfig.driverOwner);
    requireCondition(
      (await postHarness("/complete", privateConfig, facts)).status === 200,
      "harness.complete", "status",
    );
    await waitForHarnessClose(harness);
    return Object.freeze({ status: "passed", prd_id: "PRD-085", screenshot_count: 4 });
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
    try { await removeIsolatedBrowserRuntime(browserRuntimeDirectory, privateConfig.driverOwner); } catch {
      cleanupFailure ??= new BrowserAcceptanceFailure("cleanup.browser_runtime", "failed");
    }
    if (harness?.child?.exitCode === null) {
      const harnessStopped = await abortHarness(harness, privateConfig);
      // 中止失败只在没有主异常时成为最终清理错误，不能覆盖真实业务失败坐标。
      if (!harnessStopped) {
        cleanupFailure ??= new BrowserAcceptanceFailure("cleanup.harness", "failed");
      }
    }
    if (!primaryFailure && cleanupFailure) throw cleanupFailure;
  }
}


/** 解析参数并输出最小成功或失败 JSON，任何原始异常内容都不得外泄。 */
async function main() {
  try {
    const { runId } = parseArguments();
    const result = await run(runId);
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
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  DEBUG_PORT,
  HARNESS_ORIGIN,
  SCREENSHOTS,
  VIEWPORTS,
  abortHarness,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  createDriverDirectory,
  createIsolatedBrowserRuntime,
  createTracker,
  parseArguments,
  recordResponseStatus,
  removeDriverDirectory,
  removeIsolatedBrowserRuntime,
  requestContractsExact,
  safeRunId,
  validateCompletionFacts,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main());
}
