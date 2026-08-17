/**
 * PRD-066 PAGE-093 企业 Chat 真实 Edge 验收器。
 *
 * 脚本只使用 Node 标准库和 Edge DevTools Protocol。浏览器 profile、缓存、崩溃目录、
 * 截图和脱敏证据全部固定在仓库的专属 artifact 目录；口令、Token、租户、Endpoint、
 * Prompt 和 Response 只允许短暂存在于当前进程内存，不得进入终端或验收证据。
 */

import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { lstat, mkdir, realpath, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";

/** 从正式脚本位置推导仓库根，禁止调用目录改变证据归属。 */
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
/** PRD-066 浏览器证据唯一允许的固定父目录。 */
const ARTIFACT_ROOT = path.join(REPOSITORY_ROOT, ".artifacts", "prd066-browser");
/** PAGE-093 冻结的桌面和移动端验收视口。 */
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
/** Console 登录态只允许占用这两个既有 Session Storage 槽位。 */
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);
/** 撤权专项只移除企业 Chat 的后端使用权限。 */
const CHAT_PERMISSION = "chat.use";
/** 页面网络只能访问三个固定本地服务。 */
const ALLOWED_ORIGINS = new Set([
  "http://localhost:8000",
  "http://localhost:8001",
  "http://localhost:5173",
]);
/** URL 查询不得携带身份、凭据、正文或内部路由信息。 */
const FORBIDDEN_QUERY_KEY = /tenant|role|user|password|token|secret|authorization|prompt|response|endpoint|provider|model/i;
/** 权限 code 只接受产品目录使用的规范字符集。 */
const SAFE_PERMISSION_CODE = /^[a-z][a-z0-9_.-]{1,80}$/;


/** 使用固定 stage/code 表达失败，禁止外部异常正文进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存源码内固定分类，不保存 URL、DOM、响应或系统错误。 */
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 失败关闭断言；stage/code 必须是源码内固定常量。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** 折叠未知浏览器异常，避免敏感上下文经 Error.message 或 stack 外泄。 */
async function runBrowserOperation(stage, operation) {
  try {
    return await operation();
  } catch (error) {
    // 已归类失败只传播固定 stage/code；重复包装会丢失最接近故障位置的安全诊断。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "operation_failed");
  }
}


/** 只接受固定 localhost HTTP Origin，拒绝凭据、路径、查询和 Hash。 */
function normalizeLoopbackOrigin(value, expectedPort, stage) {
  let parsed;
  try {
    parsed = new URL(String(value));
  } catch {
    throw new BrowserAcceptanceFailure(stage, "url_invalid");
  }
  requireCondition(parsed.protocol === "http:", stage, "protocol");
  requireCondition(parsed.hostname === "localhost", stage, "host");
  requireCondition(parsed.port === String(expectedPort), stage, "port");
  requireCondition(!parsed.username && !parsed.password, stage, "credentials");
  requireCondition(parsed.pathname === "/" && !parsed.search && !parsed.hash, stage, "shape");
  return parsed.origin;
}


/** 校验目录不是软链接或 Windows reparse point，防止 artifact 路径逃逸。 */
async function assertPlainDirectory(directory, stage) {
  let information;
  let canonical;
  try {
    information = await lstat(directory);
    canonical = await realpath(directory);
  } catch {
    throw new BrowserAcceptanceFailure(stage, "missing_or_unreadable");
  }
  requireCondition(information.isDirectory() && !information.isSymbolicLink(), stage, "not_plain");
  requireCondition(
    path.normalize(canonical).toLowerCase() === path.normalize(directory).toLowerCase(),
    stage,
    "reparse_point",
  );
  return directory;
}


/** 解析批次目录，只允许固定证据根下一级安全 run-id。 */
function resolveArtifactDirectory(value) {
  const target = path.resolve(String(value || ""));
  const relative = path.relative(ARTIFACT_ROOT, target);
  requireCondition(
    /^[A-Za-z0-9][A-Za-z0-9._-]{2,80}$/.test(relative)
      && !relative.includes(path.sep)
      && !path.isAbsolute(relative),
    "config.artifact_directory",
    "outside_or_nested",
  );
  return target;
}


/** 独占创建普通批次目录和全部浏览器可写子目录，历史证据不得覆盖。 */
async function createArtifactDirectory(value) {
  const target = resolveArtifactDirectory(value);
  const artifacts = path.dirname(ARTIFACT_ROOT);
  if (!existsSync(artifacts)) await mkdir(artifacts, { recursive: false });
  await assertPlainDirectory(artifacts, "config.artifacts_root");
  if (!existsSync(ARTIFACT_ROOT)) await mkdir(ARTIFACT_ROOT, { recursive: false });
  await assertPlainDirectory(ARTIFACT_ROOT, "config.artifact_root");
  requireCondition(!existsSync(target), "config.artifact_directory", "already_exists");
  await mkdir(target, { recursive: false });
  await assertPlainDirectory(target, "config.artifact_directory");
  for (const relative of [
    "screenshots",
    "profile",
    "browser-cache",
    "crash-dumps",
    "browser-home",
    path.join("browser-home", "local"),
    path.join("browser-home", "roaming"),
  ]) {
    await mkdir(path.join(target, relative), { recursive: false });
  }
  return target;
}


/** 读取固定服务、Edge、角色账号和证据目录；敏感值只驻留当前 Node 进程。 */
async function readConfiguration(environment = process.env) {
  const consoleOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_CONSOLE_URL || "http://localhost:5173",
    5173,
    "config.console",
  );
  const controlOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_CONTROL_URL || "http://localhost:8001",
    8001,
    "config.control",
  );
  const edgePath = String(environment.METAHUB_EDGE_PATH || "");
  requireCondition(path.isAbsolute(edgePath) && existsSync(edgePath), "config.edge", "missing");
  const adminPassword = String(environment.METAHUB_ACCEPTANCE_ADMIN_PASSWORD || "");
  const runtimePassword = String(environment.METAHUB_ACCEPTANCE_RUNTIME_PASSWORD || "");
  requireCondition(adminPassword.length >= 12, "config.identity", "admin_password_missing");
  requireCondition(runtimePassword.length >= 12, "config.identity", "runtime_password_missing");
  const debugPort = Number(environment.METAHUB_EDGE_DEBUG_PORT || 19066);
  requireCondition(
    Number.isSafeInteger(debugPort) && debugPort >= 19000 && debugPort <= 19999,
    "config.edge_debug_port",
    "invalid",
  );
  const artifactDirectory = await createArtifactDirectory(
    environment.METAHUB_ACCEPTANCE_ARTIFACT_DIR,
  );
  const nonce = randomUUID().replaceAll("-", "");
  const prompts = Object.freeze([
    `prd066-prompt-${nonce}-first`,
    `prd066-prompt-${nonce}-revocation`,
  ]);
  const responseCanaries = Object.freeze([
    `prd066-response-${nonce}-first`,
    `prd066-response-${nonce}-revocation`,
  ]);
  const accounts = Object.freeze({
    admin: Object.freeze({ email: "admin@metahub.local", password: adminPassword }),
    developer: Object.freeze({
      email: environment.METAHUB_ACCEPTANCE_DEVELOPER_EMAIL
        || "prd066-runtime-developer@example.invalid",
      password: runtimePassword,
    }),
  });
  return Object.freeze({
    accounts,
    adminPassword,
    artifactDirectory,
    consoleOrigin,
    controlOrigin,
    debugPort,
    edgePath,
    prompts,
    responseCanaries,
    runtimePassword,
  });
}


/**
 * 浏览器进程只继承启动必需变量，所有可能写入的用户目录都固定到本批次。
 *
 * Chrome 151 在 Windows ARM64 上若同时收到自定义 USERPROFILE 与显式 --user-data-dir，
 * 会把后者误判为默认目录并禁用远程调试。调用方可仅省略 USERPROFILE；HOME、AppData 与
 * Temp 仍留在当前批次，显式 Profile 也继续由启动参数固定，避免回落宿主浏览器数据。
 */
function buildBrowserEnvironment(
  environment,
  artifactDirectory,
  { omitUserProfile = false } = {},
) {
  const sanitized = {};
  const allowedName = /^(?:path|pathext|systemroot|windir|comspec|os|number_of_processors|processor_(?:architecture|identifier)|programdata|programfiles(?:\(x86\))?|commonprogramfiles(?:\(x86\))?|lang|lc_[a-z_]+)$/i;
  for (const [key, value] of Object.entries(environment)) {
    if (allowedName.test(key)) sanitized[key] = value;
  }
  const browserHome = path.join(artifactDirectory, "browser-home");
  const parsedHome = path.parse(browserHome);
  Object.assign(sanitized, {
    TEMP: artifactDirectory,
    TMP: artifactDirectory,
    HOME: browserHome,
    LOCALAPPDATA: path.join(browserHome, "local"),
    APPDATA: path.join(browserHome, "roaming"),
    HOMEDRIVE: parsedHome.root.replace(/[\\/]$/, ""),
    HOMEPATH: browserHome.slice(parsedHome.root.length - 1),
  });
  if (!omitUserProfile) sanitized.USERPROFILE = browserHome;
  return sanitized;
}


/** 在请求离开浏览器前拦截外部 Origin、URL 凭据和敏感 query。 */
function bindCdpRequestGuard(client, pageSession, tracker) {
  client.on("Fetch.requestPaused", ({ requestId, request }, sessionId) => {
    if (sessionId !== pageSession.sessionId || !requestId || !request?.url) return;
    let target;
    try {
      target = new URL(request.url);
    } catch {
      tracker.externalRequestCount += 1;
      client.send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }, sessionId)
        .catch(() => { tracker.edgeProcessErrorCount += 1; });
      return;
    }
    const internalProtocol = ["data:", "blob:", "about:"].includes(target.protocol);
    const forbiddenQuery = [...target.searchParams.keys()].some(
      (key) => FORBIDDEN_QUERY_KEY.test(key),
    );
    const suppliedSecret = tracker.secrets.some(
      (secret) => secret && request.url.includes(secret),
    );
    if (forbiddenQuery) tracker.forbiddenQueryCount += 1;
    // URL 凭据会进入历史、代理和服务器 access log，必须单独计为隐私违规而不是只阻断请求。
    if (suppliedSecret || target.username || target.password) {
      tracker.requestPrivacyViolationCount += 1;
    }
    if (
      !forbiddenQuery
      && !suppliedSecret
      && !target.username
      && !target.password
      && (internalProtocol || ALLOWED_ORIGINS.has(target.origin))
    ) {
      client.send("Fetch.continueRequest", { requestId }, sessionId)
        .catch(() => { tracker.edgeProcessErrorCount += 1; });
      return;
    }
    if (!internalProtocol && !ALLOWED_ORIGINS.has(target.origin)) {
      tracker.externalRequestCount += 1;
    }
    client.send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }, sessionId)
      .catch(() => { tracker.edgeProcessErrorCount += 1; });
  });
}


/** 启动隔离 headless Edge；进程输出只做内存隐私计数，永不落盘。 */
function launchEdge(config, tracker) {
  const argumentsList = [
    "--headless=new",
    ...(config.incognito === true ? ["--incognito"] : []),
    "--disable-gpu",
    // Codex 外层 Windows 沙箱会阻止 Chromium 再创建一层受限 Renderer Token；该开关只用于
    // 本验收器的一次性 headless Edge。外层沙箱、工作区 Profile 和 localhost 请求拦截仍生效，
    // 严禁把此参数复制到 Gateway、Control、Console 或任何生产浏览器启动路径。
    "--no-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-crash-reporter",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-extensions",
    // 一次性验收禁止启动系统更新安装器，避免外部子进程污染 owner 树和锁住隔离 Profile。
    "--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter,OsUpdateHandlerWin",
    "--disable-sync",
    "--metrics-recording-only",
    "--remote-allow-origins=*",
    "--remote-debugging-address=127.0.0.1",
    `--remote-debugging-port=${config.debugPort}`,
    `--user-data-dir=${path.join(config.artifactDirectory, "profile")}`,
    `--disk-cache-dir=${path.join(config.artifactDirectory, "browser-cache")}`,
    `--crash-dumps-dir=${path.join(config.artifactDirectory, "crash-dumps")}`,
    "--window-size=1280,720",
    "about:blank",
  ];
  const child = spawn(config.edgePath, argumentsList, {
    cwd: config.artifactDirectory,
    env: buildBrowserEnvironment(process.env, config.artifactDirectory, {
      omitUserProfile: config.omitUserProfile === true,
    }),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  /** 原始输出不得保存；仅判断当前已知敏感值是否出现。 */
  const inspectChunk = (chunk) => {
    const value = String(chunk ?? "");
    if (tracker.secrets.some((secret) => secret && value.includes(secret))) {
      tracker.processPrivacyViolationCount += 1;
    }
  };
  child.stdout.on("data", inspectChunk);
  child.stderr.on("data", inspectChunk);
  child.on("error", () => { tracker.edgeProcessErrorCount += 1; });
  return child;
}


/** 轮询固定本地调试端点并返回 Browser WebSocket 地址。 */
async function waitForDebugger(debugPort, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/version`);
      if (response.ok) {
        const payload = await response.json();
        if (typeof payload.webSocketDebuggerUrl === "string") return payload.webSocketDebuggerUrl;
      }
    } catch {
      // Edge 启动期连接失败是预期轮询状态，不能输出系统异常正文。
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new BrowserAcceptanceFailure("browser.debugger", "timeout");
}


/** 等待新建 Page target 出现在本地调试目录，并返回该 Page 自己的 WebSocket。 */
async function waitForPageDebugger(debugPort, targetId, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      if (response.ok) {
        const targets = await response.json();
        const target = Array.isArray(targets)
          ? targets.find((item) => item?.id === targetId && item?.type === "page")
          : null;
        if (typeof target?.webSocketDebuggerUrl === "string") {
          return target.webSocketDebuggerUrl;
        }
      }
    } catch {
      // Page target 建立期短暂不可见是正常竞争，不输出包含调试 Endpoint 的网络异常。
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new BrowserAcceptanceFailure("browser.page_debugger", "timeout");
}


/** 最小 CDP 客户端只实现 PAGE-093 所需命令、事件与有界超时。 */
class CdpClient {
  /** 创建尚未连接的客户端。 */
  constructor(webSocketUrl) {
    this.webSocketUrl = webSocketUrl;
    this.socket = null;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
  }

  /** 建立 Browser 级 WebSocket，并安装固定消息分发。 */
  async connect() {
    this.socket = new WebSocket(this.webSocketUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new BrowserAcceptanceFailure("browser.cdp.connect", "timeout")),
        10000,
      );
      this.socket.addEventListener("open", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
      this.socket.addEventListener("error", () => {
        clearTimeout(timer);
        reject(new BrowserAcceptanceFailure("browser.cdp.connect", "failed"));
      }, { once: true });
    });
    this.socket.addEventListener("message", (event) => this.#dispatch(event.data));
    this.socket.addEventListener("close", () => {
      for (const item of this.pending.values()) {
        item.reject(new BrowserAcceptanceFailure(item.stage, "closed"));
      }
      this.pending.clear();
    });
  }

  /** 发送一条 Browser 或扁平 Page Session 命令。 */
  send(method, params = {}, sessionId = undefined, timeoutMs = 20000) {
    const stage = `browser.cdp.${method}`;
    requireCondition(this.socket?.readyState === WebSocket.OPEN, stage, "not_open");
    const id = this.nextId;
    this.nextId += 1;
    const message = { id, method, params };
    // 扁平 CDP 的 Page 命令必须携带精确 Session；Browser 级命令则不能伪造 Session owner。
    if (sessionId) message.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new BrowserAcceptanceFailure(stage, "timeout"));
      }, timeoutMs);
      this.pending.set(id, {
        stage,
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
      this.socket.send(JSON.stringify(message));
    });
  }

  /** 注册 CDP 事件监听器；回调只消费内存协议对象。 */
  on(method, listener) {
    const listeners = this.listeners.get(method) ?? [];
    listeners.push(listener);
    this.listeners.set(method, listeners);
  }

  /** 主动关闭协议连接；重复关闭不覆盖验收结论。 */
  close() {
    try {
      this.socket?.close();
    } catch {
      // Browser.close 可能已关闭 WebSocket。
    }
  }

  /** 分发命令响应或事件；协议错误统一折叠，禁止原始正文外泄。 */
  #dispatch(raw) {
    let message;
    try {
      const value = typeof raw === "string" ? raw : Buffer.from(raw).toString("utf8");
      message = JSON.parse(value);
    } catch {
      return;
    }
    if (message.id) {
      const item = this.pending.get(message.id);
      if (!item) return;
      this.pending.delete(message.id);
      // CDP 原始 error 可能带页面值或 URL，只转换成固定 protocol 分类交给对应调用方。
      if (message.error) item.reject(new BrowserAcceptanceFailure(item.stage, "protocol"));
      else item.resolve(message.result ?? {});
      return;
    }
    if (!message.method) return;
    for (const listener of this.listeners.get(message.method) ?? []) {
      try {
        listener(message.params ?? {}, message.sessionId);
      } catch {
        // 监听异常通过最终计数失败关闭，不输出协议对象。
      }
    }
  }
}


/** 构造 Page target 参数；私有 Context ID 必须原样绑定，不能回落默认 Profile。 */
function createPageTargetParameters(browserContextId = null) {
  if (browserContextId !== null) {
    requireCondition(
      typeof browserContextId === "string" && browserContextId.length > 0,
      "browser.context",
      "invalid",
    );
  }
  return Object.freeze({
    url: "about:blank",
    ...(browserContextId === null ? {} : { browserContextId }),
  });
}


/** 新建独占 Page target、直连其 WebSocket 并启用所需 CDP 域。 */
async function createPageSession(browserClient, debugPort, browserContextId = null) {
  const { targetId } = await browserClient.send(
    "Target.createTarget",
    createPageTargetParameters(browserContextId),
  );
  requireCondition(typeof targetId === "string" && targetId, "browser.target", "missing");
  if (browserContextId !== null) {
    const { targetInfo } = await browserClient.send("Target.getTargetInfo", { targetId });
    requireCondition(
      targetInfo?.browserContextId === browserContextId,
      "browser.context",
      "target_mismatch",
    );
  }
  const pageWebSocketUrl = await waitForPageDebugger(debugPort, targetId);
  const pageClient = new CdpClient(pageWebSocketUrl);
  await pageClient.connect();
  try {
    await pageClient.send("Page.enable");
    await pageClient.send("Runtime.enable");
    await pageClient.send("Network.enable");
    await pageClient.send("Network.setCacheDisabled", { cacheDisabled: true });
    await pageClient.send("Log.enable");
    await pageClient.send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
  } catch (error) {
    // 启用任一 Page 域失败都关闭本 target 连接，不能遗留半初始化事件监听器。
    pageClient.close();
    throw error;
  }
  // 直连 Page WebSocket 时事件没有扁平 sessionId；undefined 本身就是不可伪造的 owner。
  return Object.freeze({
    client: pageClient,
    targetId,
    sessionId: undefined,
    browserContextId,
  });
}


/** 在页面主世界执行只返回 by-value 低风险事实的表达式。 */
async function evaluate(client, sessionId, expression, stage = "browser.evaluate") {
  const result = await client.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, sessionId);
  requireCondition(!result.exceptionDetails, stage, "exception");
  return result.result?.value;
}


/** 有界等待 DOM 条件；表达式不得把正文返回给错误或终端。 */
async function waitForDom(client, sessionId, expression, stage, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    // 只读取 Boolean，命中后立即结束轮询，禁止把受保护正文作为等待结果带回 Node。
    if (await evaluate(client, sessionId, `Boolean(${expression})`, stage)) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new BrowserAcceptanceFailure(stage, "timeout");
}


/** 有界等待网络追踪计数，避免只看缓存 DOM 形成假通过。 */
async function waitForTrackerCount(readCount, expected, stage, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (readCount() >= expected) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new BrowserAcceptanceFailure(stage, "timeout");
}


/** 响应事件没有 method，按 requestId 读取后立即释放关联。 */
function consumeTrackedRequestMethod(methods, requestId) {
  const method = methods.get(requestId) ?? "";
  methods.delete(requestId);
  return method;
}


/** 导航或刷新前预先监听 load，防止极快本地页面错过事件。 */
async function runAndWaitForPageLoad(client, sessionId, operation, stage, timeoutMs = 30000) {
  let loaded = false;
  const observed = new Promise((resolve) => {
    client.on("Page.loadEventFired", (_, eventSessionId) => {
      // 只接受当前 Page Session 的首个 load，其他 Target 或重复事件不能提前完成验收。
      if (!loaded && eventSessionId === sessionId) {
        loaded = true;
        resolve(true);
      }
    });
  });
  await operation();
  const completed = await Promise.race([
    observed,
    new Promise((resolve) => setTimeout(() => resolve(false), timeoutMs)),
  ]);
  requireCondition(completed === true, stage, "load_timeout");
}


/** 构造只保存状态码、计数和内存敏感值的浏览器追踪器。 */
function createTracker(config) {
  const accountEmails = Object.values(config.accounts).map((item) => item.email);
  const credentials = [config.adminPassword, config.runtimePassword, ...accountEmails];
  const runtimePlaintexts = [...config.prompts, ...config.responseCanaries];
  return {
    browserTokens: new Set(),
    chatClearStatuses: [],
    chatCreateStatuses: [],
    chatMessageStatuses: [],
    chatOptionsStatuses: [],
    consoleErrorCount: 0,
    consolePrivacyViolationCount: 0,
    credentials,
    edgeProcessErrorCount: 0,
    externalRequestCount: 0,
    forbiddenQueryCount: 0,
    loginStatuses: [],
    logoutStatuses: [],
    navigationStatuses: [],
    processPrivacyViolationCount: 0,
    requestMethods: new Map(),
    requestPrivacyViolationCount: 0,
    runtimePlaintexts,
    secrets: [...credentials, ...runtimePlaintexts],
  };
}


/** 把真实 assistant 正文加入内存扫描集合；值永不进入 evidence。 */
function registerRuntimePlaintext(tracker, value) {
  const normalized = String(value ?? "");
  requireCondition(
    normalized.length > 0 && normalized.length <= 65536,
    "browser.chat.response",
    "plaintext_shape",
  );
  if (!tracker.runtimePlaintexts.includes(normalized)) tracker.runtimePlaintexts.push(normalized);
  // assistant 正文加入通用 secrets 集合后，URL、日志、Storage 和非消息请求会共享同一零泄漏门禁。
  if (!tracker.secrets.includes(normalized)) tracker.secrets.push(normalized);
  return normalized;
}


/** 不区分 Header 大小写读取单值，避免依赖 CDP 的字段规范化方式。 */
function readHeader(headers, name) {
  const expected = String(name).toLowerCase();
  for (const [key, value] of Object.entries(headers ?? {})) {
    if (key.toLowerCase() === expected) return String(value ?? "");
  }
  return "";
}


/** 判断当前路径是否为企业 Chat 消息写接口。 */
function isChatMessagePath(pathname) {
  return /^\/chat\/sessions\/[A-Za-z0-9_-]{1,128}\/messages$/.test(pathname);
}


/** 判断当前路径是否为企业 Chat 会话删除接口。 */
function isChatSessionPath(pathname) {
  return /^\/chat\/sessions\/[A-Za-z0-9_-]{1,128}$/.test(pathname);
}


/**
 * 注册网络、Console 和 Log 追踪。
 *
 * 只保留 HTTP 数字、固定路由分类和违规计数；请求 Header、正文、URL 与日志文本均不持久化。
 */
function bindTracker(client, pageSession, config, tracker) {
  client.on("Network.requestWillBeSent", ({ requestId, request }, sessionId) => {
    // 浏览器后台 Target 不属于业务页面，若混入会污染请求方法关联和隐私计数。
    if (sessionId !== pageSession.sessionId || !request?.url) return;
    if (typeof requestId === "string" && typeof request.method === "string") {
      tracker.requestMethods.set(requestId, request.method);
    }
    let target;
    try {
      target = new URL(request.url);
    } catch {
      tracker.externalRequestCount += 1;
      return;
    }
    const internalProtocol = ["data:", "blob:", "about:"].includes(target.protocol);
    if (!internalProtocol && !ALLOWED_ORIGINS.has(target.origin)) {
      tracker.externalRequestCount += 1;
    }
    if ([...target.searchParams.keys()].some((key) => FORBIDDEN_QUERY_KEY.test(key))) {
      tracker.forbiddenQueryCount += 1;
    }
    // 任一内存凭据或正文进入 URL 都属于可持久化泄漏，即使请求随后被 Fetch 层阻断也必须计数。
    if (tracker.secrets.some((secret) => secret && request.url.includes(secret))) {
      tracker.requestPrivacyViolationCount += 1;
    }
    const authorization = readHeader(request.headers, "authorization");
    const bearer = authorization.match(/^Bearer ([^\s]+)$/)?.[1] ?? "";
    // 动态 Bearer 只保留在内存扫描集合，用于证明它没有转移到 URL、正文或非 Control Origin。
    if (bearer) tracker.browserTokens.add(bearer);
    // Console Session 只能发送给冻结的 Control Origin，发往 Gateway/Console/外网均视为越权泄漏。
    if (authorization && target.origin !== config.controlOrigin) {
      tracker.requestPrivacyViolationCount += 1;
    }
    const body = String(request.postData ?? "");
    // 口令和账号只允许在登录 POST；Prompt history 只允许在消息 POST，其他正文全部禁止。
    const loginRequest = target.origin === config.controlOrigin
      && target.pathname === "/auth/login" && request.method === "POST";
    const messageRequest = target.origin === config.controlOrigin
      && isChatMessagePath(target.pathname) && request.method === "POST";
    const prohibited = loginRequest
      ? tracker.runtimePlaintexts
      : (messageRequest ? tracker.credentials : tracker.secrets);
    // 每类 POST 只允许其业务必需明文，其他凭据或正文出现在 body 中即失败关闭。
    if (prohibited.some((secret) => secret && body.includes(secret))) {
      tracker.requestPrivacyViolationCount += 1;
    }
    for (const token of tracker.browserTokens) {
      // Bearer 只能存在于 Authorization Header，复制到 URL 或正文会被网络设施长期留存。
      if (token && (request.url.includes(token) || body.includes(token))) {
        tracker.requestPrivacyViolationCount += 1;
      }
    }
  });

  client.on("Network.responseReceived", ({ requestId, response }, sessionId) => {
    // 响应必须与当前页面 Session 绑定，后台 Target 的同名路由不能改变业务状态计数。
    if (sessionId !== pageSession.sessionId || !response?.url) return;
    const method = consumeTrackedRequestMethod(tracker.requestMethods, requestId);
    let target;
    try {
      target = new URL(response.url);
    } catch {
      return;
    }
    if (target.origin !== config.controlOrigin) return;
    const status = Number(response.status);
    // 登录/注销分别计数，才能证明敏感 Session 生命周期在浏览器验收中完整闭合。
    if (target.pathname === "/auth/login" && method === "POST") tracker.loginStatuses.push(status);
    if (target.pathname === "/auth/logout" && method === "POST") tracker.logoutStatuses.push(status);
    if (target.pathname === "/console/navigation" && method === "GET") {
      tracker.navigationStatuses.push(status);
    }
    if (target.pathname === "/chat/options" && method === "GET") {
      tracker.chatOptionsStatuses.push(status);
    }
    // 创建必须精确绑定 POST /chat/sessions，不能把同路径 GET 或预检请求误算为业务成功。
    if (target.pathname === "/chat/sessions" && method === "POST") {
      tracker.chatCreateStatuses.push(status);
    }
    if (isChatMessagePath(target.pathname) && method === "POST") {
      tracker.chatMessageStatuses.push(status);
    }
    // 清空只接受具体 Session 的 DELETE，避免导航或消息请求意外满足清理验收。
    if (isChatSessionPath(target.pathname) && method === "DELETE") {
      tracker.chatClearStatuses.push(status);
    }
  });

  client.on("Network.loadingFailed", ({ requestId }, sessionId) => {
    // 失败请求也要释放 method 关联，防止 requestId 残留被后续响应复用成假业务状态。
    if (sessionId === pageSession.sessionId) tracker.requestMethods.delete(requestId);
  });

  /** Console 原文只在回调栈比较，既不保存也不输出。 */
  const inspectConsoleText = (text, level) => {
    const value = String(text ?? "");
    if (tracker.secrets.some((secret) => secret && value.includes(secret))) {
      tracker.consolePrivacyViolationCount += 1;
    }
    // error/assert 即使没有敏感正文也表示页面运行异常，必须独立阻断最终通过。
    if (["error", "assert"].includes(String(level))) tracker.consoleErrorCount += 1;
  };
  client.on("Runtime.consoleAPICalled", ({ type, args }, sessionId) => {
    // 只检查当前业务 Page 的 Console；Edge 内部页面噪音不能影响或掩盖验收结论。
    if (sessionId !== pageSession.sessionId) return;
    inspectConsoleText(
      (args ?? []).map((item) => item.value ?? item.description ?? "").join(" "),
      type,
    );
  });
  client.on("Runtime.exceptionThrown", (_, sessionId) => {
    // 当前 Page 的未捕获异常无论正文为何都算失败，但异常对象本身不得持久化。
    if (sessionId === pageSession.sessionId) tracker.consoleErrorCount += 1;
  });
  client.on("Log.entryAdded", ({ entry }, sessionId) => {
    // 浏览器 Log 域与 Runtime Console 使用相同内存扫描，避免只覆盖一种日志旁路。
    if (sessionId === pageSession.sessionId) inspectConsoleText(entry?.text, entry?.level);
  });
}


/** 通过真实登录表单建立 Developer Session，口令只进入输入框和登录 POST。 */
async function loginDeveloper(client, sessionId, config, tracker) {
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.login.form");
  const account = config.accounts.developer;
  const expectedLoginCount = tracker.loginStatuses.length + 1;
  const values = JSON.stringify({ email: account.email, password: account.password });
  const submitted = await evaluate(client, sessionId, `(() => {
    const values = ${values};
    const form = document.querySelector("#login-form");
    if (!form) return false;
    const setValue = (name, value) => {
      const input = form.querySelector('[name="' + name + '"]');
      if (!input) return false;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    };
    // 三个登录字段必须全部写入成功后才提交，避免残缺 owner 触发错误账号或旧值重用。
    if (!setValue("tenantSlug", "default") || !setValue("email", values.email)
      || !setValue("password", values.password)) return false;
    form.requestSubmit();
    return true;
  })()`, "browser.login.submit");
  requireCondition(submitted === true, "browser.login", "submit");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector("#logout-button")
      && document.querySelector('[data-console-nav="enterprise-chat"]')`,
    "browser.login.shell",
  );
  await waitForTrackerCount(
    () => tracker.loginStatuses.length,
    expectedLoginCount,
    "browser.login.network",
  );
  requireCondition(
    tracker.loginStatuses[expectedLoginCount - 1] === 200,
    "browser.login.network",
    "status",
  );
}


/** 点击后端已授权导航并等待 options 真实 200 和 PAGE-093 根节点。 */
async function openEnterpriseChat(client, sessionId, tracker, stage = "browser.chat.open") {
  const expectedOptionsCount = tracker.chatOptionsStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-console-nav="enterprise-chat"]');
    if (!button) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "navigation_missing");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector("[data-enterprise-chat-root]")
      && document.querySelector("[data-enterprise-chat-project]")
      && document.querySelector("[data-enterprise-chat-alias]")`,
    `${stage}.dom`,
  );
  await waitForTrackerCount(
    () => tracker.chatOptionsStatuses.length,
    expectedOptionsCount,
    `${stage}.options`,
  );
  requireCondition(
    tracker.chatOptionsStatuses[expectedOptionsCount - 1] === 200,
    `${stage}.options`,
    "status",
  );
  const locationSafe = await evaluate(
    client,
    sessionId,
    `location.origin === "http://localhost:5173" && location.pathname === "/"
      && location.search === "" && location.hash === "#enterprise-chat"`,
    `${stage}.location`,
  );
  requireCondition(locationSafe === true, `${stage}.location`, "unsafe");
}


/** 点击新建并等待会话 POST 201；会话 ID 不读取、不保存。 */
async function createChatSession(client, sessionId, tracker, stage) {
  const expectedCreateCount = tracker.chatCreateStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector("[data-enterprise-chat-new]");
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector("[data-enterprise-chat-clear]:not([disabled])")
      && !document.querySelector("[data-enterprise-chat-form] textarea:disabled")`,
    `${stage}.dom`,
  );
  await waitForTrackerCount(
    () => tracker.chatCreateStatuses.length,
    expectedCreateCount,
    `${stage}.network`,
  );
  requireCondition(
    tracker.chatCreateStatuses[expectedCreateCount - 1] === 201,
    `${stage}.network`,
    "status",
  );
}


/** 构造含唯一 canary 的真实 Prompt，便于证明正文只在允许传输面出现。 */
function chatPrompt(config, index) {
  return `请仅回复 ${config.responseCanaries[index]}。私密输入标记：${config.prompts[index]}`;
}


/** 输入并发送一次真实消息，校验两条纯文本消息和治理摘要。 */
async function sendChatMessage(client, sessionId, config, tracker, index, stage) {
  const prompt = registerRuntimePlaintext(tracker, chatPrompt(config, index));
  const expectedMessageCount = tracker.chatMessageStatuses.length + 1;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector("[data-enterprise-chat-form]");
    const textarea = form?.querySelector("textarea[name='message']");
    if (!form || !textarea || textarea.disabled) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    setter.call(textarea, ${JSON.stringify(prompt)});
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    form.requestSubmit();
    return true;
  })()`, `${stage}.submit`);
  requireCondition(submitted === true, stage, "submit");
  await waitForDom(
    client,
    sessionId,
    `document.querySelectorAll("[data-enterprise-chat-message-index]").length === 2
      && document.querySelector(".enterprise-chat-governance")
      && !document.querySelector("[data-enterprise-chat-form] textarea:disabled")`,
    `${stage}.dom`,
    60000,
  );
  await waitForTrackerCount(
    () => tracker.chatMessageStatuses.length,
    expectedMessageCount,
    `${stage}.network`,
    60000,
  );
  requireCondition(
    tracker.chatMessageStatuses[expectedMessageCount - 1] === 200,
    `${stage}.network`,
    "status",
  );
  const facts = await evaluate(client, sessionId, `(() => {
    const messages = [...document.querySelectorAll("[data-enterprise-chat-message-index]")];
    const bodies = messages.map((item) => item.querySelector(".enterprise-chat-message-body")?.textContent || "");
    const governance = document.querySelector(".enterprise-chat-governance");
    const requestId = governance?.querySelector(".enterprise-chat-request-id dd")?.textContent || "";
    return {
      prompt_matches: bodies[0] === ${JSON.stringify(prompt)},
      response: bodies[1] || "",
      roles_match: messages[0]?.dataset.role === "user" && messages[1]?.dataset.role === "assistant",
      governance_count: document.querySelectorAll(".enterprise-chat-governance").length,
      governance_field_count: governance?.querySelectorAll("dt").length || 0,
      request_id_valid: requestId.length > 0 && requestId !== "-",
      textarea_empty: (document.querySelector("[data-enterprise-chat-form] textarea")?.value || "") === "",
    };
  })()`, `${stage}.facts`);
  requireCondition(facts.prompt_matches && facts.roles_match, stage, "message_projection");
  requireCondition(
    facts.governance_count === 1 && facts.governance_field_count === 6 && facts.request_id_valid,
    stage,
    "governance",
  );
  requireCondition(facts.textarea_empty, stage, "draft_not_cleared");
  const response = registerRuntimePlaintext(tracker, facts.response);
  await assertActivePlaintextBoundary(client, sessionId, tracker, `${stage}.privacy`);
  return Object.freeze({ prompt, response });
}


/** 活动会话正文只能存在于消息 body 文本节点，不得进入 URL、属性或 Storage。 */
async function assertActivePlaintextBoundary(client, sessionId, tracker, stage) {
  const needles = JSON.stringify(tracker.runtimePlaintexts.filter((value) => value.length > 0));
  const facts = await evaluate(client, sessionId, `(() => {
    const needles = ${needles};
    const localEntries = Object.entries(localStorage);
    const sessionEntries = Object.entries(sessionStorage);
    const storageText = JSON.stringify([...localEntries, ...sessionEntries]);
    const attributes = [...document.querySelectorAll("*")].flatMap((element) => (
      [...element.attributes].map((attribute) => attribute.value)
    ));
    let outsideMessageHits = 0;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (needles.some((needle) => needle && node.nodeValue.includes(needle))
        && !node.parentElement?.closest(".enterprise-chat-message-body")) outsideMessageHits += 1;
    }
    return {
      outside_message_hits: outsideMessageHits,
      attribute_hits: needles.filter((needle) => attributes.some((value) => value.includes(needle))).length,
      url_hits: needles.filter((needle) => location.href.includes(needle)).length,
      storage_hits: needles.filter((needle) => storageText.includes(needle)).length,
      message_count: document.querySelectorAll("[data-enterprise-chat-message-index]").length,
    };
  })()`, stage);
  requireCondition(facts.message_count === 2, stage, "message_count");
  requireCondition(facts.outside_message_hits === 0, stage, "outside_message");
  requireCondition(facts.attribute_hits === 0, stage, "attribute");
  requireCondition(facts.url_hits === 0 && facts.storage_hits === 0, stage, "persistence");
  return facts;
}


/** 点击清空并等待 DELETE 204，随后证明 Session UI、草稿和正文同步消失。 */
async function clearChatSession(client, sessionId, tracker, stage) {
  const expectedClearCount = tracker.chatClearStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector("[data-enterprise-chat-clear]");
    if (!button || button.disabled) return false;
    window.confirm = () => true;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    `document.querySelectorAll("[data-enterprise-chat-message-index]").length === 0
      && document.querySelector("[data-enterprise-chat-new]:not([disabled])")
      && document.querySelector("[data-enterprise-chat-clear]:disabled")`,
    `${stage}.dom`,
  );
  await waitForTrackerCount(
    () => tracker.chatClearStatuses.length,
    expectedClearCount,
    `${stage}.network`,
  );
  requireCondition(
    tracker.chatClearStatuses[expectedClearCount - 1] === 204,
    `${stage}.network`,
    "status",
  );
  await assertPlaintextPurged(client, sessionId, tracker, `${stage}.privacy`, true);
}


/** 校验全部运行期正文 canary 已从 URL、DOM 与 Storage 清除。 */
async function assertPlaintextPurged(client, sessionId, tracker, stage, expectRoot) {
  const needles = JSON.stringify(tracker.runtimePlaintexts.filter((value) => value.length > 0));
  const facts = await evaluate(client, sessionId, `(() => {
    const needles = ${needles};
    const dom = document.documentElement.outerHTML;
    const storage = JSON.stringify([...Object.entries(localStorage), ...Object.entries(sessionStorage)]);
    return {
      hit_count: needles.filter((needle) => needle
        && [location.href, dom, storage].some((value) => value.includes(needle))).length,
      root_count: document.querySelectorAll("[data-enterprise-chat-root]").length,
      message_count: document.querySelectorAll("[data-enterprise-chat-message-index]").length,
      draft_length: (document.querySelector("[data-enterprise-chat-form] textarea")?.value || "").length,
      session_badge_count: document.querySelectorAll(".enterprise-chat-session-state").length,
    };
  })()`, stage);
  requireCondition(facts.hit_count === 0, stage, "plaintext_hit");
  requireCondition(facts.root_count === (expectRoot ? 1 : 0), stage, "root");
  requireCondition(
    facts.message_count === 0 && facts.draft_length === 0 && facts.session_badge_count === 0,
    stage,
    "conversation_state",
  );
  return facts;
}


/** 设置固定设备指标，并等待两帧让媒体查询和布局稳定。 */
async function setViewport(client, sessionId, viewport) {
  await client.send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: false,
    screenWidth: viewport.width,
    screenHeight: viewport.height,
  }, sessionId);
  await evaluate(
    client,
    sessionId,
    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))",
    `browser.viewport.${viewport.name}`,
  );
}


/** 读取 PAGE-093 低风险布局事实，不返回正文、身份或 request_id。 */
async function readLayoutMetrics(client, sessionId) {
  return evaluate(client, sessionId, `(() => {
    const root = document.querySelector("[data-enterprise-chat-root]");
    const toolbar = root?.querySelector(".enterprise-chat-toolbar");
    const title = root?.querySelector(".enterprise-chat-title");
    const selectors = root?.querySelector(".enterprise-chat-selectors");
    const tools = root?.querySelector(".enterprise-chat-tools");
    const messages = root?.querySelector(".enterprise-chat-messages");
    const composer = root?.querySelector(".enterprise-chat-composer");
    const draft = root?.querySelector(".enterprise-chat-draft");
    const send = root?.querySelector(".enterprise-chat-send");
    const selectElements = [...(root?.querySelectorAll("select") || [])];
    const messageElements = [...(root?.querySelectorAll(".enterprise-chat-message") || [])];
    const governanceElements = [...(root?.querySelectorAll(".enterprise-chat-governance") || [])];
    const requestIds = [...(root?.querySelectorAll(".enterprise-chat-request-id dd") || [])];
    // 任一关键节点缺失或横向溢出都按布局失败处理，不能把空 DOM 当成“无溢出”。
    const overflow = (element) => !element || element.scrollWidth > element.clientWidth + 1;
    // 只把两个非空矩形的实际交叠视为冲突，边界相接的一像素舍入不误报。
    const overlaps = (left, right) => {
      if (!left || !right) return false;
      const a = left.getBoundingClientRect();
      const b = right.getBoundingClientRect();
      return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
        && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
    };
    const pairs = [
      [title, selectors],
      [selectors, tools],
      [toolbar, messages],
      [messages, composer],
      [draft, send],
    ];
    const rootRect = root?.getBoundingClientRect();
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      root_visible: Boolean(rootRect) && rootRect.width > 0 && rootRect.height > 0,
      root_inside_horizontal: Boolean(rootRect)
        && rootRect.left >= -1 && rootRect.right <= innerWidth + 1,
      root_overflow: root ? Math.max(0, root.scrollWidth - root.clientWidth) : 1,
      toolbar_overflow_count: overflow(toolbar) ? 1 : 0,
      select_count: selectElements.length,
      select_overflow_count: selectElements.filter(overflow).length,
      composer_overflow_count: overflow(composer) ? 1 : 0,
      message_count: messageElements.length,
      message_overflow_count: messageElements.filter(overflow).length,
      governance_count: governanceElements.length,
      governance_overflow_count: governanceElements.filter(overflow).length,
      request_id_wrap_safe: requestIds.length === 1 && requestIds.every((element) => (
        element.scrollWidth <= element.clientWidth + 1
        && getComputedStyle(element).overflowWrap === "anywhere"
      )),
      overlap_count: pairs.filter(([left, right]) => overlaps(left, right)).length,
    };
  })()`, "browser.layout.read");
}


/** 校验双视口稳定边界，不允许横向溢出、控件遮挡或长 request_id 撑宽。 */
function assertLayoutMetrics(metrics, viewport) {
  const stage = `browser.layout.${viewport.name}`;
  requireCondition(
    metrics.viewport_width === viewport.width && metrics.viewport_height === viewport.height,
    stage,
    "viewport",
  );
  requireCondition(
    metrics.document_scroll_width <= viewport.width + 1
      && metrics.body_scroll_width <= viewport.width + 1,
    stage,
    "horizontal_overflow",
  );
  requireCondition(
    metrics.root_visible && metrics.root_inside_horizontal && metrics.root_overflow <= 1,
    stage,
    "root",
  );
  requireCondition(
    metrics.toolbar_overflow_count === 0
      && metrics.select_count === 2
      && metrics.select_overflow_count === 0,
    stage,
    "toolbar",
  );
  requireCondition(metrics.composer_overflow_count === 0, stage, "composer");
  requireCondition(
    metrics.message_count === 2 && metrics.message_overflow_count === 0,
    stage,
    "messages",
  );
  requireCondition(
    metrics.governance_count === 1 && metrics.governance_overflow_count === 0,
    stage,
    "governance",
  );
  requireCondition(metrics.request_id_wrap_safe === true, stage, "request_id_wrap");
  requireCondition(metrics.overlap_count === 0, stage, "overlap");
  return metrics;
}


/**
 * 捕获脱敏截图并校验 PNG 非空像素。
 *
 * 截图前把两条正文替换为固定占位、隐藏用户身份，并用长非敏感 request_id 做响应式压力测试；
 * 原正文仍只在页面状态闭包中，随后由真实 DELETE 清除。
 */
async function captureViewport(client, sessionId, config, viewport) {
  await setViewport(client, sessionId, viewport);
  const prepared = await evaluate(client, sessionId, `(() => {
    const root = document.querySelector("[data-enterprise-chat-root]");
    if (!root) return false;
    root.scrollIntoView({ block: "start", inline: "nearest" });
    for (const body of root.querySelectorAll(".enterprise-chat-message-body")) {
      body.textContent = "消息正文已脱敏";
    }
    const requestId = root.querySelector(".enterprise-chat-request-id dd");
    if (requestId) requestId.textContent = "req_prd066_" + "a".repeat(180);
    const identity = document.querySelector(".user-menu > div");
    if (identity) identity.style.visibility = "hidden";
    return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))));
  })()`, `browser.screenshot.${viewport.name}.prepare`);
  requireCondition(prepared === true, `browser.screenshot.${viewport.name}`, "prepare");
  const layout = assertLayoutMetrics(await readLayoutMetrics(client, sessionId), viewport);
  const screenshot = await client.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  }, sessionId, 30000);
  const buffer = Buffer.from(screenshot.data, "base64");
  const filename = `page-093-${viewport.name}.png`;
  await writeFile(path.join(config.artifactDirectory, "screenshots", filename), buffer);
  const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${viewport.name}`);
  return Object.freeze({
    viewport: viewport.name,
    content_anchor: "enterprise_chat",
    filename,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    byte_length: buffer.length,
    plaintext_redacted: true,
    request_id_stress: true,
    ...pixels,
    layout,
  });
}


/** 构造不含身份的操作 request_id，只用于 Control 响应链路核对。 */
function operationRequestId(operation) {
  return `prd066_browser_${operation}_${randomUUID().replaceAll("-", "")}`;
}


/** 调用固定 Control Origin 并校验 HTTP、JSON、request_id 与 trace_id 合同。 */
async function controlJson(
  config,
  pathname,
  { method = "GET", token = "", payload = null, operation },
  stage,
) {
  const headers = { "X-Request-ID": operationRequestId(operation) };
  // 管理员 Bearer 只在撤权专项请求的 Header 中使用，匿名调用明确保持无 Authorization。
  if (token) headers.Authorization = `Bearer ${token}`;
  if (payload !== null) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(`${config.controlOrigin}${pathname}`, {
      method,
      headers,
      body: payload === null ? undefined : JSON.stringify(payload),
      redirect: "error",
      signal: AbortSignal.timeout(20000),
    });
  } catch {
    throw new BrowserAcceptanceFailure(stage, "network");
  }
  let body;
  try {
    body = await response.json();
  } catch {
    throw new BrowserAcceptanceFailure(stage, "json");
  }
  requireCondition(response.status === 200, stage, "status");
  requireCondition(
    typeof body?.request_id === "string" && body.request_id.length > 0
      && body.request_id === response.headers.get("x-request-id"),
    stage,
    "request_id",
  );
  requireCondition(
    typeof body?.trace_id === "string" && body.trace_id.length > 0
      && body.trace_id === response.headers.get("x-trace-id"),
    stage,
    "trace_id",
  );
  return body;
}


/** 创建只驻内存的管理员 API Session，专用于 Developer 撤权和恢复。 */
async function openAdminControlSession(config) {
  const body = await controlJson(config, "/auth/login", {
    method: "POST",
    payload: {
      tenant_slug: "default",
      email: config.accounts.admin.email,
      password: config.accounts.admin.password,
    },
    operation: "permission_admin_login",
  }, "browser.permission_admin.login");
  const token = String(body.access_token ?? "");
  requireCondition(
    token.length >= 16 && body.user?.role === "admin",
    "browser.permission_admin.login",
    "identity",
  );
  // 高权限 Token 只返回当前函数调用链，finally 会无条件注销且证据从不保存其值。
  return token;
}


/** 注销撤权专项管理员 Session，避免遗留高权限会话。 */
async function closeAdminControlSession(config, token) {
  const body = await controlJson(config, "/auth/logout", {
    method: "POST",
    token,
    operation: "permission_admin_logout",
  }, "browser.permission_admin.logout");
  requireCondition(body.logged_out === true, "browser.permission_admin.logout", "result");
}


/** 权限集合只接受非空、无重复的安全 code，并返回冻结排序副本。 */
function normalizePermissionCodes(value, stage) {
  requireCondition(Array.isArray(value) && value.length > 0, stage, "permission_codes");
  requireCondition(value.every((item) => typeof item === "string"), stage, "permission_codes");
  requireCondition(
    value.every((code) => SAFE_PERMISSION_CODE.test(code))
      && new Set(value).size === value.length,
    stage,
    "permission_codes",
  );
  return Object.freeze([...value].sort());
}


/** 比较两个已规范化权限集合，仅决定 finally 是否需要执行恢复写。 */
function samePermissionCodes(left, right) {
  return left.length === right.length && left.every((code, index) => code === right[index]);
}


/** 读取当前租户 Developer 角色的 ID、revision 和完整权限集合。 */
async function readDeveloperRoleSnapshot(config, token) {
  const body = await controlJson(config, "/permissions", {
    token,
    operation: "permission_matrix_read",
  }, "browser.permission_matrix.read");
  const matches = Array.isArray(body.roles)
    ? body.roles.filter((item) => item?.code === "developer")
    : [];
  requireCondition(matches.length === 1, "browser.permission_matrix.read", "developer_role");
  const role = matches[0];
  const roleId = String(role.role_id ?? "");
  const revision = Number(role.permission_revision);
  requireCondition(/^[A-Za-z0-9_-]{1,120}$/.test(roleId), "browser.permission_matrix.read", "role_id");
  requireCondition(
    Number.isSafeInteger(revision) && revision >= 1,
    "browser.permission_matrix.read",
    "revision",
  );
  return Object.freeze({
    roleId,
    revision,
    permissionCodes: normalizePermissionCodes(
      role.permission_codes,
      "browser.permission_matrix.read",
    ),
  });
}


/** 用服务端 revision CAS 完整替换 Developer 权限并核对写后权威集合。 */
async function replaceDeveloperRoleSnapshot(
  config,
  token,
  snapshot,
  permissionCodes,
  operation,
) {
  const stage = `browser.permission_matrix.${operation}`;
  const expectedCodes = normalizePermissionCodes(permissionCodes, stage);
  const body = await controlJson(
    config,
    `/roles/${encodeURIComponent(snapshot.roleId)}/permissions`,
    {
      method: "PATCH",
      token,
      payload: {
        expected_revision: snapshot.revision,
        permission_codes: expectedCodes,
      },
      operation: `permission_${operation}`,
    },
    stage,
  );
  requireCondition(body.changed === true && body.role?.code === "developer", stage, "changed");
  const updatedCodes = normalizePermissionCodes(body.role.permission_codes, stage);
  requireCondition(samePermissionCodes(updatedCodes, expectedCodes), stage, "write_projection");
  const revision = Number(body.role.permission_revision);
  requireCondition(
    Number.isSafeInteger(revision) && revision === snapshot.revision + 1,
    stage,
    "revision",
  );
  // 返回冻结副本供下一次 CAS 使用，禁止调用方继续引用已过期的旧 revision/permission 集合。
  return Object.freeze({ roleId: snapshot.roleId, revision, permissionCodes: updatedCodes });
}


/** 刷新当前 Developer 页面并等待后端导航完成一次真实 200。 */
async function reloadForPermissionChange(client, sessionId, tracker, expression, stage) {
  const expectedNavigationCount = tracker.navigationStatuses.length + 1;
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.reload", { ignoreCache: true }, sessionId),
    `${stage}.reload`,
  );
  await waitForDom(client, sessionId, expression, `${stage}.dom`);
  await waitForTrackerCount(
    () => tracker.navigationStatuses.length,
    expectedNavigationCount,
    `${stage}.navigation`,
  );
  requireCondition(
    tracker.navigationStatuses[expectedNavigationCount - 1] === 200,
    `${stage}.navigation`,
    "status",
  );
}


/**
 * 在同一 Developer 浏览器 Session 中撤销 ``chat.use``，验证失败关闭后恢复原集合。
 *
 * 权限恢复和管理员注销使用嵌套 finally；任何浏览器断言失败都不能污染共享开发租户。
 */
async function exerciseDeveloperPermissionRevocation(client, sessionId, config, tracker) {
  let adminToken = "";
  let original = null;
  let revocationVerified = false;
  const optionsBeforeRevocation = tracker.chatOptionsStatuses.length;
  try {
    adminToken = await openAdminControlSession(config);
    original = await readDeveloperRoleSnapshot(config, adminToken);
    requireCondition(
      original.permissionCodes.includes(CHAT_PERMISSION),
      "browser.permission_revocation",
      "required_permission_missing",
    );
    const revokedCodes = original.permissionCodes.filter((code) => code !== CHAT_PERMISSION);
    await replaceDeveloperRoleSnapshot(config, adminToken, original, revokedCodes, "revoke");
    await reloadForPermissionChange(
      client,
      sessionId,
      tracker,
      `document.querySelector("#logout-button")
        && !document.querySelector('[data-console-nav="enterprise-chat"]')
        && !document.querySelector("[data-enterprise-chat-root]")
        && location.hash !== "#enterprise-chat"`,
      "browser.permission_revocation.revoked",
    );
    // 后端导航完成后再给异步任务一个短窗口；撤权页面不得偷偷预取受保护 options。
    await new Promise((resolve) => setTimeout(resolve, 500));
    requireCondition(
      tracker.chatOptionsStatuses.length === optionsBeforeRevocation,
      "browser.permission_revocation.revoked",
      "options_after_revoke",
    );
    const fallbackSafe = await evaluate(client, sessionId, `(() => ({
      hash_safe: location.hash === "" || location.hash === "#overview",
      logged_in: Boolean(document.querySelector("#logout-button")),
    }))()`, "browser.permission_revocation.revoked");
    requireCondition(
      fallbackSafe.hash_safe && fallbackSafe.logged_in,
      "browser.permission_revocation.revoked",
      "fallback",
    );
    await assertPlaintextPurged(
      client,
      sessionId,
      tracker,
      "browser.permission_revocation.revoked.privacy",
      false,
    );
    revocationVerified = true;
  } finally {
    try {
      // 只有管理员会话和原矩阵都建立成功时才尝试恢复，避免用残缺快照覆盖真实权限。
      if (adminToken && original) {
        const current = await readDeveloperRoleSnapshot(config, adminToken);
        // 撤权流程已被其他操作恢复时不再写一次 CAS，避免无意义 revision 漂移。
        if (!samePermissionCodes(current.permissionCodes, original.permissionCodes)) {
          await replaceDeveloperRoleSnapshot(
            config,
            adminToken,
            current,
            original.permissionCodes,
            "restore",
          );
        }
      }
    } finally {
      // 管理员 Session 无论恢复成功与否都必须注销，防止验收失败遗留高权限登录态。
      if (adminToken) await closeAdminControlSession(config, adminToken);
    }
  }
  requireCondition(
    revocationVerified,
    "browser.permission_revocation",
    "revocation_not_verified",
  );
  await reloadForPermissionChange(
    client,
    sessionId,
    tracker,
    `document.querySelector("#logout-button")
      && document.querySelector('[data-console-nav="enterprise-chat"]')
      && !document.querySelector("[data-enterprise-chat-root]")`,
    "browser.permission_revocation.restored",
  );
  await openEnterpriseChat(client, sessionId, tracker, "browser.permission_revocation.reopened");
  return Object.freeze({
    permission_removed: CHAT_PERMISSION,
    protected_dom_cleared: true,
    plaintext_cleared: true,
    options_blocked_while_revoked: true,
    hash_fallback: true,
    permission_restored: true,
    entry_reopened: true,
    same_browser_session: true,
    backend_revision_cas: true,
  });
}


/** 通过真实按钮注销，并证明认证槽位和 PAGE-093 DOM 同步销毁。 */
async function logoutDeveloper(client, sessionId, tracker) {
  const expectedLogoutCount = tracker.logoutStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector("#logout-button");
    if (!button) return false;
    button.click();
    return true;
  })()`, "browser.logout");
  requireCondition(clicked === true, "browser.logout", "button_missing");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.logout.form");
  await waitForTrackerCount(
    () => tracker.logoutStatuses.length,
    expectedLogoutCount,
    "browser.logout.network",
  );
  requireCondition(
    tracker.logoutStatuses[expectedLogoutCount - 1] === 200,
    "browser.logout.network",
    "status",
  );
  const cleared = await evaluate(client, sessionId, `(() => ({
    auth_slots: ${JSON.stringify(AUTH_STORAGE_KEYS)}.filter(
      (key) => sessionStorage.getItem(key) !== null,
    ).length,
    protected_roots: document.querySelectorAll("[data-enterprise-chat-root]").length,
    messages: document.querySelectorAll("[data-enterprise-chat-message-index]").length,
  }))()`, "browser.logout.cleanup");
  requireCondition(cleared.auth_slots === 0, "browser.logout", "auth_slots");
  requireCondition(
    cleared.protected_roots === 0 && cleared.messages === 0,
    "browser.logout",
    "protected_dom",
  );
}


/** 扫描最终 URL、DOM 与 Storage，并复核网络、Console 和进程计数。 */
async function assertBrowserPrivacy(client, sessionId, config, tracker) {
  const supplied = JSON.stringify([
    ...tracker.secrets,
    ...tracker.browserTokens,
  ].filter((value) => value.length > 0));
  const metrics = await evaluate(client, sessionId, `(() => {
    const secrets = ${supplied};
    const html = document.documentElement.outerHTML;
    const localEntries = Object.entries(localStorage);
    const sessionEntries = Object.entries(sessionStorage);
    const storageText = JSON.stringify([...localEntries, ...sessionEntries]);
    return {
      location_ok: location.origin === "http://localhost:5173"
        && location.pathname === "/" && location.search === "",
      local_storage_count: localEntries.length,
      session_storage_count: sessionEntries.length,
      secret_hit_count: secrets.filter((secret) => secret
        && [location.href, html, storageText].some((value) => value.includes(secret))).length,
      protected_root_count: document.querySelectorAll("[data-enterprise-chat-root]").length,
      message_count: document.querySelectorAll("[data-enterprise-chat-message-index]").length,
      login_form_count: document.querySelectorAll("#login-form").length,
    };
  })()`, "browser.privacy");
  requireCondition(metrics.location_ok, "browser.privacy", "location");
  requireCondition(metrics.local_storage_count === 0, "browser.privacy", "local_storage");
  requireCondition(metrics.session_storage_count === 0, "browser.privacy", "session_storage");
  requireCondition(metrics.secret_hit_count === 0, "browser.privacy", "secret_hit");
  requireCondition(
    metrics.protected_root_count === 0 && metrics.message_count === 0
      && metrics.login_form_count === 1,
    "browser.privacy",
    "protected_dom",
  );
  requireCondition(tracker.externalRequestCount === 0, "browser.privacy", "external_request");
  requireCondition(tracker.forbiddenQueryCount === 0, "browser.privacy", "query");
  requireCondition(
    tracker.requestPrivacyViolationCount === 0,
    "browser.privacy",
    "request",
  );
  requireCondition(
    tracker.consolePrivacyViolationCount === 0,
    "browser.privacy",
    "console_privacy",
  );
  requireCondition(
    tracker.processPrivacyViolationCount === 0,
    "browser.privacy",
    "process_privacy",
  );
  requireCondition(tracker.consoleErrorCount === 0, "browser.privacy", "console_error");
  requireCondition(tracker.edgeProcessErrorCount === 0, "browser.privacy", "edge_error");
  return Object.freeze({
    url_hits: 0,
    dom_hits: 0,
    storage_hits: 0,
    console_hits: 0,
    process_hits: 0,
    external_request_count: 0,
    sensitive_query_count: 0,
  });
}


/** 核对主流程每条 Chat 接口的精确次数与状态，额外调用和隐式重试都失败。 */
function assertNetworkContracts(tracker) {
  const exactStatuses = (actual, expected, stage) => {
    requireCondition(
      actual.length === expected.length
        && actual.every((status, index) => status === expected[index]),
      stage,
      "statuses",
    );
  };
  exactStatuses(tracker.loginStatuses, [200], "browser.network.login");
  exactStatuses(tracker.logoutStatuses, [200], "browser.network.logout");
  exactStatuses(tracker.chatOptionsStatuses, [200, 200], "browser.network.options");
  exactStatuses(tracker.chatCreateStatuses, [201, 201], "browser.network.create");
  exactStatuses(tracker.chatMessageStatuses, [200, 200], "browser.network.message");
  exactStatuses(tracker.chatClearStatuses, [204], "browser.network.clear");
  requireCondition(
    tracker.navigationStatuses.length >= 4
      && tracker.navigationStatuses.every((status) => status === 200),
    "browser.network.navigation",
    "statuses",
  );
  return Object.freeze({
    login_200_count: 1,
    logout_200_count: 1,
    options_200_count: 2,
    session_201_count: 2,
    message_200_count: 2,
    clear_204_count: 1,
    navigation_200_count: tracker.navigationStatuses.length,
    implicit_retry_count: 0,
  });
}


/** 递归删除本批次浏览器状态目录，保留截图与 JSON 证据。 */
async function removeSensitiveBrowserState(config) {
  const artifactCanonical = await realpath(config.artifactDirectory);
  for (const name of ["profile", "browser-cache", "crash-dumps", "browser-home", "Importer_0_4"]) {
    const target = path.join(config.artifactDirectory, name);
    if (!existsSync(target)) continue;
    await assertPlainDirectory(target, `cleanup.${name}`);
    const canonical = await realpath(target);
    requireCondition(
      canonical.toLowerCase().startsWith(`${artifactCanonical.toLowerCase()}${path.sep}`),
      `cleanup.${name}`,
      "outside_artifact",
    );
    let removed = false;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      try {
        await rm(target, { recursive: true, force: false });
        removed = true;
        break;
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    }
    requireCondition(removed && !existsSync(target), `cleanup.${name}`, "remove_failed");
  }
}


/** 等待 Edge 退出；超时后只终止本脚本创建且仍绑定的子进程。 */
async function waitForChildExit(child, timeoutMs = 10000) {
  if (child.exitCode !== null) return;
  const exited = await Promise.race([
    new Promise((resolve) => child.once("exit", () => resolve(true))),
    new Promise((resolve) => setTimeout(() => resolve(false), timeoutMs)),
  ]);
  if (!exited && child.exitCode === null) child.kill();
  if (!exited) {
    await Promise.race([
      new Promise((resolve) => child.once("exit", resolve)),
      new Promise((resolve) => setTimeout(resolve, 5000)),
    ]);
  }
}


/** 写入 JSON 前扫描口令、账号、正文、Token、租户值与 Endpoint。 */
async function publishEvidence(config, tracker, evidence) {
  const serialized = `${JSON.stringify(evidence, null, 2)}\n`;
  const forbiddenValues = [
    ...tracker.secrets,
    ...tracker.browserTokens,
    ...Object.values(config.accounts).map((item) => item.email),
    "default",
    config.consoleOrigin,
    config.controlOrigin,
  ];
  requireCondition(
    forbiddenValues.every((value) => !value || !serialized.includes(value)),
    "evidence",
    "sensitive_value",
  );
  await writeFile(path.join(config.artifactDirectory, "browser-evidence.json"), serialized, "utf8");
}


/** 执行登录、两轮 Chat、双视口、清空、撤权恢复、隐私和注销验收。 */
async function executeAcceptance(config) {
  const tracker = createTracker(config);
  let child = null;
  let browserClient = null;
  let client = null;
  let pageSession = null;
  try {
    child = launchEdge(config, tracker);
    const webSocketUrl = await waitForDebugger(config.debugPort);
    browserClient = new CdpClient(webSocketUrl);
    await browserClient.connect();
    pageSession = await createPageSession(browserClient, config.debugPort);
    client = pageSession.client;
    bindCdpRequestGuard(client, pageSession, tracker);
    bindTracker(client, pageSession, config, tracker);
    await runAndWaitForPageLoad(
      client,
      pageSession.sessionId,
      () => client.send(
        "Page.navigate",
        { url: `${config.consoleOrigin}/#overview` },
        pageSession.sessionId,
      ),
      "browser.initial_page",
    );
    await loginDeveloper(client, pageSession.sessionId, config, tracker);
    await openEnterpriseChat(client, pageSession.sessionId, tracker);

    await createChatSession(client, pageSession.sessionId, tracker, "browser.chat.first.create");
    await sendChatMessage(
      client,
      pageSession.sessionId,
      config,
      tracker,
      0,
      "browser.chat.first.send",
    );
    const screenshots = [];
    for (const viewport of VIEWPORTS) {
      screenshots.push(await captureViewport(
        client,
        pageSession.sessionId,
        config,
        viewport,
      ));
    }
    await setViewport(client, pageSession.sessionId, VIEWPORTS[0]);
    await clearChatSession(client, pageSession.sessionId, tracker, "browser.chat.first.clear");

    await createChatSession(client, pageSession.sessionId, tracker, "browser.chat.second.create");
    await sendChatMessage(
      client,
      pageSession.sessionId,
      config,
      tracker,
      1,
      "browser.chat.second.send",
    );
    const permissionRevocation = await exerciseDeveloperPermissionRevocation(
      client,
      pageSession.sessionId,
      config,
      tracker,
    );
    await logoutDeveloper(client, pageSession.sessionId, tracker);
    const privacy = await assertBrowserPrivacy(
      client,
      pageSession.sessionId,
      config,
      tracker,
    );
    const requests = assertNetworkContracts(tracker);
    const evidence = Object.freeze({
      status: "passed",
      prd_id: "PRD-066",
      page_id: "PAGE-093",
      workflow: Object.freeze({
        authenticated_role: "developer",
        session_create_count: 2,
        governed_message_count: 2,
        clear_verified: true,
        active_plaintext_boundary_verified: true,
        plaintext_purge_verified: true,
        logout_cleanup_verified: true,
      }),
      permission_revocation: permissionRevocation,
      privacy,
      requests,
      screenshots,
    });
    await publishEvidence(config, tracker, evidence);
    return evidence;
  } finally {
    if (browserClient) {
      try {
        await browserClient.send("Browser.close");
      } catch {
        // 连接可能已由 Browser.close 关闭；清理不得覆盖真实失败阶段。
      }
      browserClient.close();
    }
    if (client) {
      client.close();
    }
    if (child) await waitForChildExit(child);
    if (config?.artifactDirectory && existsSync(config.artifactDirectory)) {
      await removeSensitiveBrowserState(config);
    }
  }
}


/** CLI 只输出固定成功摘要或固定失败分类，不输出敏感上下文。 */
async function main() {
  try {
    const config = await readConfiguration();
    const evidence = await runBrowserOperation(
      "browser.acceptance",
      () => executeAcceptance(config),
    );
    process.stdout.write(`${JSON.stringify({
      status: "passed",
      prd_id: evidence.prd_id,
      page_id: evidence.page_id,
      screenshot_count: evidence.screenshots.length,
      message_count: evidence.workflow.governed_message_count,
    })}\n`);
    return 0;
  } catch (error) {
    const failure = error instanceof BrowserAcceptanceFailure
      ? { stage: error.stage, code: error.code }
      : { stage: "browser.acceptance", code: "failed" };
    process.stderr.write(`${JSON.stringify({ status: "failed", ...failure })}\n`);
    return 1;
  }
}


export {
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  CdpClient,
  VIEWPORTS,
  assertLayoutMetrics,
  bindCdpRequestGuard,
  buildBrowserEnvironment,
  createPageSession,
  createPageTargetParameters,
  consumeTrackedRequestMethod,
  evaluate,
  launchEdge,
  normalizeLoopbackOrigin,
  normalizePermissionCodes,
  requireCondition,
  resolveArtifactDirectory,
  runAndWaitForPageLoad,
  runBrowserOperation,
  samePermissionCodes,
  setViewport,
  waitForChildExit,
  waitForDebugger,
  waitForDom,
  waitForTrackerCount,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
