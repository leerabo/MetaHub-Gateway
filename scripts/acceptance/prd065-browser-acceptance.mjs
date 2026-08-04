/**
 * PRD-065 PAGE-092 多角色首页真实 Edge 验收器。
 *
 * Browser 插件不可用时，本脚本只使用 Node 标准库和 Edge DevTools Protocol 驱动本机 Edge。
 * 浏览器 profile、临时目录、截图和脱敏证据全部固定在 D:/MetaHub/.artifacts/prd065-browser，
 * 也不把密码、Session Token、租户标识、Endpoint 或页面正文写入证据。
 */

import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { lstat, mkdir, realpath, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";

/** 脚本和仓库根目录只从正式源码位置推导，调用方当前目录不能改变证据归属。 */
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
/** 浏览器证据唯一允许的固定父目录。 */
const ARTIFACT_ROOT = path.join(REPOSITORY_ROOT, ".artifacts", "prd065-browser");
/** PAGE-092 冻结的桌面与移动验收视口。 */
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
/** Console 登录态只允许使用现有两个 Session Storage 槽位。 */
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);
/** 六角色的默认视图、视图数量和首卡片是 Excel/设计文档冻结合同。 */
const ROLE_EXPECTATIONS = Object.freeze({
  admin: Object.freeze({ selected: "executive", viewCount: 5, firstCard: "cost" }),
  developer: Object.freeze({ selected: "developer", viewCount: 3, firstCard: "adoption" }),
  viewer: Object.freeze({ selected: "executive", viewCount: 1, firstCard: "cost" }),
  finance_admin: Object.freeze({ selected: "finance", viewCount: 2, firstCard: "cost" }),
  security_admin: Object.freeze({ selected: "security", viewCount: 2, firstCard: "security" }),
  ops_admin: Object.freeze({ selected: "operations", viewCount: 2, firstCard: "provider_health" }),
});
/** 财务角色被撤销任一财务视图必需权限后，只允许安全回落到经营总览。 */
const REVOKED_FINANCE_EXPECTATION = Object.freeze({
  selected: "executive",
  viewCount: 1,
  firstCard: "cost",
});
/** 撤权专项只移除财务视图的一个必需权限，恢复时仍使用完整原集合和 revision CAS。 */
const FINANCE_VIEW_REVOCATION_PERMISSION = "billing.anomaly.read";
/** 页面网络只能访问本地 Gateway、Control 与 Console，不允许连接外部站点。 */
const ALLOWED_ORIGINS = new Set([
  "http://localhost:8000",
  "http://localhost:8001",
  "http://localhost:5173",
]);
/** role-view query 只允许后端已下发的单一 view，身份字段一律禁止。 */
const FORBIDDEN_QUERY_KEY = /tenant|role|user|password|token|secret|authorization|prompt|response|endpoint/i;


/** 使用固定 stage/code 表达失败，避免 Edge、CDP、URL 或页面正文进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存源码内固定分类，禁止拼接外部异常正文。 */
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 失败关闭断言；stage/code 只能传源码内固定常量。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** 把未知浏览器异常折叠为固定错误，避免证据或 stdout 泄露敏感上下文。 */
async function runBrowserOperation(stage, operation) {
  try {
    return await operation();
  } catch (error) {
    // 已归类错误保留固定 stage/code；其他浏览器错误统一折叠，不能输出原始 URL 或 DOM。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "operation_failed");
  }
}


/** 只接受固定 localhost Origin，拒绝凭据、路径、查询和 Hash。 */
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


/** 校验现有目录不是软链接或 Windows reparse point，防止证据路径逃逸。 */
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


/** 独占创建普通批次目录及全部浏览器可写子目录，历史证据不得被覆盖。 */
async function createArtifactDirectory(value) {
  const target = resolveArtifactDirectory(value);
  const artifacts = path.dirname(ARTIFACT_ROOT);
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


/** 读取固定服务、Edge、角色账号和证据目录；敏感值只保存在当前 Node 进程内存。 */
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
  const debugPort = Number(environment.METAHUB_EDGE_DEBUG_PORT || 19065);
  requireCondition(
    Number.isSafeInteger(debugPort) && debugPort >= 19000 && debugPort <= 19999,
    "config.edge_debug_port",
    "invalid",
  );
  const artifactDirectory = await createArtifactDirectory(
    environment.METAHUB_ACCEPTANCE_ARTIFACT_DIR,
  );
  const accounts = Object.freeze({
    admin: Object.freeze({ email: "admin@metahub.local", password: adminPassword }),
    developer: Object.freeze({ email: "prd065-runtime-developer@example.invalid", password: runtimePassword }),
    viewer: Object.freeze({ email: "prd065-runtime-viewer@example.invalid", password: runtimePassword }),
    finance_admin: Object.freeze({ email: "prd065-runtime-finance_admin@example.invalid", password: runtimePassword }),
    security_admin: Object.freeze({ email: "prd065-runtime-security_admin@example.invalid", password: runtimePassword }),
    ops_admin: Object.freeze({ email: "prd065-runtime-ops_admin@example.invalid", password: runtimePassword }),
  });
  return Object.freeze({
    accounts,
    adminPassword,
    artifactDirectory,
    canary: "prd065-sensitive-endpoint-canary",
    consoleOrigin,
    controlOrigin,
    debugPort,
    edgePath,
    runtimePassword,
  });
}


/** 浏览器进程只继承 Windows 启动白名单，并把所有可写根固定到本批次。 */
function buildBrowserEnvironment(environment, artifactDirectory) {
  const sanitized = {};
  const allowedName = /^(?:path|pathext|systemroot|windir|comspec|os|number_of_processors|processor_(?:architecture|identifier)|programdata|programfiles(?:\(x86\))?|commonprogramfiles(?:\(x86\))?|homedrive|homepath|home|userprofile|localappdata|appdata|lang|lc_[a-z_]+)$/i;
  for (const [key, value] of Object.entries(environment)) {
    if (allowedName.test(key)) sanitized[key] = value;
  }
  // Edge 运行库需要从系统用户目录读取组件；实际可写 profile、缓存、崩溃和临时目录由启动参数及 TEMP/TMP 固定。
  Object.assign(sanitized, {
    TEMP: artifactDirectory,
    TMP: artifactDirectory,
  });
  return sanitized;
}


/**
 * 在请求离开浏览器前通过 CDP Fetch 域拦截外部 Origin、URL 凭据与敏感查询。
 * Network 追踪器仍复核已放行请求；两层边界共同证明页面未访问外部站点。
 */
function bindCdpRequestGuard(client, pageSession, tracker) {
  client.on("Fetch.requestPaused", ({ requestId, request }, sessionId) => {
    // 只处理当前 PAGE Session 的完整请求；浏览器后台 Target 和残缺事件不属于业务页面验收范围。
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
    // 任何凭据进入 URL 都会落入代理、历史或服务日志，因此必须单独记为隐私违规。
    if (suppliedSecret) tracker.requestPrivacyViolationCount += 1;
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


/** 启动隔离 headless Edge；stdout/stderr 只做内存隐私计数，不写入证据。 */
function launchEdge(config, tracker) {
  const profile = path.join(config.artifactDirectory, "profile");
  const argumentsList = [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-crash-reporter",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-extensions",
    "--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter",
    "--disable-sync",
    "--metrics-recording-only",
    "--remote-allow-origins=*",
    "--remote-debugging-address=127.0.0.1",
    `--remote-debugging-port=${config.debugPort}`,
    `--user-data-dir=${profile}`,
    `--disk-cache-dir=${path.join(config.artifactDirectory, "browser-cache")}`,
    `--crash-dumps-dir=${path.join(config.artifactDirectory, "crash-dumps")}`,
    "--window-size=1280,720",
    "about:blank",
  ];
  const child = spawn(config.edgePath, argumentsList, {
    cwd: config.artifactDirectory,
    env: buildBrowserEnvironment(process.env, config.artifactDirectory),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  const secrets = [config.adminPassword, config.runtimePassword, config.canary];
  /** Edge 进程输出只做敏感值命中计数，原始行不得持久化。 */
  const inspectChunk = (chunk) => {
    const text = String(chunk ?? "");
    // 任何内存凭据出现在进程输出都属于验收失败，不能把命中行复制到终端。
    if (secrets.some((secret) => secret && text.includes(secret))) {
      tracker.processPrivacyViolationCount += 1;
    }
  };
  child.stdout.on("data", inspectChunk);
  child.stderr.on("data", inspectChunk);
  child.on("error", () => {
    tracker.edgeProcessErrorCount += 1;
  });
  return child;
}


/** 轮询固定本地调试端点，返回 Browser 级 WebSocket 地址。 */
async function waitForDebugger(debugPort, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/version`);
      if (response.ok) {
        const payload = await response.json();
        if (typeof payload.webSocketDebuggerUrl === "string") return payload.webSocketDebuggerUrl;
      }
    } catch {
      // Edge 启动期间连接失败是预期轮询状态，不能把系统错误正文带入验收输出。
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new BrowserAcceptanceFailure("browser.debugger", "timeout");
}


/** 最小 CDP 客户端只实现 PAGE-092 验收所需命令、事件和超时，不保存协议正文。 */
class CdpClient {
  /** 创建尚未连接的 CDP 客户端。 */
  constructor(webSocketUrl) {
    this.webSocketUrl = webSocketUrl;
    this.socket = null;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
  }

  /** 建立 Browser 级 WebSocket，并注册固定消息分发。 */
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

  /** 发送一条带超时的 Browser 或扁平 Session 命令。 */
  send(method, params = {}, sessionId = undefined, timeoutMs = 20000) {
    const stage = `browser.cdp.${method}`;
    // 连接若在某条命令前关闭，必须保留具体 method，便于只复测失败节点而不是重跑整套验收。
    requireCondition(this.socket?.readyState === WebSocket.OPEN, stage, "not_open");
    const id = this.nextId;
    this.nextId += 1;
    const message = { id, method, params };
    // 扁平 Session 命令必须携带当前页面 sessionId，Browser 级命令才允许省略。
    if (sessionId) message.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new BrowserAcceptanceFailure(stage, "timeout"));
      }, timeoutMs);
      this.pending.set(id, {
        stage,
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
      this.socket.send(JSON.stringify(message));
    });
  }

  /** 注册一类 CDP 事件监听，回调只接收内存协议对象。 */
  on(method, listener) {
    const listeners = this.listeners.get(method) ?? [];
    listeners.push(listener);
    this.listeners.set(method, listeners);
  }

  /** 主动关闭协议连接，不把关闭异常覆盖验收结论。 */
  close() {
    try {
      this.socket?.close();
    } catch {
      // Browser.close 可能先关闭 WebSocket；重复关闭不属于验收失败。
    }
  }

  /** 分发命令响应或事件；协议 error 统一折叠，不泄露原始 CDP 正文。 */
  #dispatch(raw) {
    let message;
    try {
      const text = typeof raw === "string" ? raw : Buffer.from(raw).toString("utf8");
      message = JSON.parse(text);
    } catch {
      return;
    }
    if (message.id) {
      const item = this.pending.get(message.id);
      if (!item) return;
      this.pending.delete(message.id);
      // CDP 原始错误可能含页面上下文，统一转换为无正文 protocol 分类。
      if (message.error) item.reject(new BrowserAcceptanceFailure(item.stage, "protocol"));
      else item.resolve(message.result ?? {});
      return;
    }
    if (!message.method) return;
    for (const listener of this.listeners.get(message.method) ?? []) {
      try {
        listener(message.params ?? {}, message.sessionId);
      } catch {
        // 监听器只累计安全事实；单条异常由最终计数合同失败关闭，不输出协议正文。
      }
    }
  }
}


/** 选择隔离 Edge 的空白页、附加扁平 Session，并启用验收所需 CDP 域。 */
async function createPageSession(client) {
  const targets = await client.send("Target.getTargets");
  let targetId = targets.targetInfos?.find(
    (item) => item.type === "page" && item.url === "about:blank",
  )?.targetId;
  if (!targetId) {
    ({ targetId } = await client.send("Target.createTarget", { url: "about:blank" }));
  }
  requireCondition(typeof targetId === "string" && targetId.length > 0, "browser.target", "missing");
  const attached = await client.send("Target.attachToTarget", { targetId, flatten: true });
  const sessionId = attached.sessionId;
  requireCondition(typeof sessionId === "string" && sessionId.length > 0, "browser.session", "missing");
  for (const method of ["Page.enable", "Runtime.enable", "Network.enable", "Log.enable", "Fetch.enable"]) {
    await client.send(method, {}, sessionId);
  }
  await client.send("Emulation.setDeviceMetricsOverride", {
    width: VIEWPORTS[0].width,
    height: VIEWPORTS[0].height,
    deviceScaleFactor: 1,
    mobile: false,
    screenWidth: VIEWPORTS[0].width,
    screenHeight: VIEWPORTS[0].height,
  }, sessionId);
  // 首次导航必须由调用方在安装 Network/Console 监听后执行，否则首屏外连可能逃过隐私审计。
  // 只返回当前页面内部标识供后续命令路由，浏览器连接地址不进入证据。
  return Object.freeze({ sessionId, targetId });
}


/** 在当前主执行上下文求值并只返回 JSON 可序列化结果。 */
async function evaluate(client, sessionId, expression) {
  const response = await client.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  }, sessionId);
  requireCondition(!response.exceptionDetails, "browser.evaluate", "exception");
  return response.result?.value;
}


/** 轮询 DOM 条件，迟到页面或网络响应不能靠固定 sleep 冒充成功。 */
async function waitForDom(client, sessionId, expression, stage, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ready = await evaluate(client, sessionId, `Boolean(${expression})`);
    if (ready) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new BrowserAcceptanceFailure(stage, "timeout");
}


/** 等待内存追踪计数达到目标值，避免页面已渲染但访问事件仍在途时立即注销 Session。 */
async function waitForTrackerCount(readCount, expected, stage, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (readCount() >= expected) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new BrowserAcceptanceFailure(stage, "timeout");
}


/** 读取 Header 时忽略大小写，只判断去向，不保存认证值。 */
function readHeader(headers, name) {
  const match = Object.entries(headers ?? {}).find(([key]) => key.toLowerCase() === name);
  return match ? String(match[1] ?? "") : "";
}


/**
 * 按 CDP requestId 取出并删除请求方法。
 * responseReceived 本身没有 method，消费后立即删除可防止长时验收的内存映射无界增长。
 */
function consumeTrackedRequestMethod(methods, requestId) {
  const method = methods.get(requestId) ?? "";
  methods.delete(requestId);
  return method;
}


/** 创建浏览器网络、Console 与敏感值追踪器。 */
function createTracker(config) {
  return {
    consoleErrorCount: 0,
    consolePrivacyViolationCount: 0,
    edgeProcessErrorCount: 0,
    eventStatuses: [],
    externalRequestCount: 0,
    forbiddenQueryCount: 0,
    loginStatuses: [],
    logoutStatuses: [],
    navigationStatuses: [],
    processPrivacyViolationCount: 0,
    requestEventTypes: new Map(),
    requestMethods: new Map(),
    requestPrivacyViolationCount: 0,
    roleViewedEventStatuses: [],
    roleViewStatuses: [],
    roleViewQueries: [],
    secrets: [config.adminPassword, config.runtimePassword, config.canary],
  };
}


/** 注册浏览器事件；只累计状态和违规计数，不持久化 URL、Header、正文或 Token。 */
function bindTracker(client, pageSession, config, tracker) {
  client.on("Network.requestWillBeSent", ({ requestId, request }, sessionId) => {
    if (sessionId !== pageSession.sessionId || !request?.url) return;
    // responseReceived 不携带 HTTP method，必须按 CDP requestId 关联本次请求；重定向会覆盖为最新 method。
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
    if (!["data:", "blob:", "about:"].includes(target.protocol)
      && !ALLOWED_ORIGINS.has(target.origin)) {
      tracker.externalRequestCount += 1;
    }
    if ([...target.searchParams.keys()].some((key) => FORBIDDEN_QUERY_KEY.test(key))) {
      tracker.forbiddenQueryCount += 1;
    }
    // URL 中出现任一内存凭据或 canary 都必须失败，不能依赖页面最终是否渲染。
    if (tracker.secrets.some((secret) => secret && request.url.includes(secret))) {
      tracker.requestPrivacyViolationCount += 1;
    }
    const authorization = readHeader(request.headers, "authorization");
    // Authorization 只允许发送到固定 Control Origin，静态资源和外部地址均不得携带。
    if (authorization && target.origin !== config.controlOrigin) {
      tracker.requestPrivacyViolationCount += 1;
    }
    const body = String(request.postData ?? "");
    const isLogin = target.origin === config.controlOrigin && target.pathname === "/auth/login";
    const bodySecrets = isLogin ? [config.canary] : tracker.secrets;
    // 登录 POST 是口令唯一合法传输面；其他请求正文不得携带任何凭据或 canary。
    if (bodySecrets.some((secret) => secret && body.includes(secret))) {
      tracker.requestPrivacyViolationCount += 1;
    }
    if (
      target.origin === config.controlOrigin
      && target.pathname === "/console/events"
      && request.method === "POST"
      && typeof requestId === "string"
    ) {
      try {
        const payload = JSON.parse(body);
        // 只保存事件类型这个低风险枚举，不保存 payload、页面路径或身份字段。
        tracker.requestEventTypes.set(requestId, String(payload?.event_type ?? ""));
      } catch {
        // 非 JSON 事件仍由后端返回错误；追踪器不记录原始正文。
      }
    }
    // role-view query 另存为键值结构供最终 exact-set 校验，不保存 Header 或响应正文。
    if (target.origin === config.controlOrigin && target.pathname === "/dashboard/role-view") {
      tracker.roleViewQueries.push([...target.searchParams.entries()]);
    }
  });
  client.on("Network.responseReceived", ({ requestId, response }, sessionId) => {
    // 只处理当前 PAGE target 的响应，浏览器后台 target 不能污染业务状态计数。
    if (sessionId !== pageSession.sessionId || !response?.url) return;
    const eventType = tracker.requestEventTypes.get(requestId) ?? "";
    tracker.requestEventTypes.delete(requestId);
    const method = consumeTrackedRequestMethod(tracker.requestMethods, requestId);
    let target;
    try {
      target = new URL(response.url);
    } catch {
      return;
    }
    if (target.origin !== config.controlOrigin) return;
    const status = Number(response.status);
    // 登录状态只保存 HTTP 数字，不保存认证响应或 Token。
    if (target.pathname === "/auth/login" && method === "POST") tracker.loginStatuses.push(status);
    // 登出状态用于证明后端 Session 已撤销，不保存请求 Header。
    if (target.pathname === "/auth/logout" && method === "POST") tracker.logoutStatuses.push(status);
    if (target.pathname === "/console/navigation" && method === "GET") {
      tracker.navigationStatuses.push(status);
    }
    // role-view 每次都必须实际命中 Control；状态之外的卡片正文由 DOM 白名单验证。
    if (target.pathname === "/dashboard/role-view" && method === "GET") {
      tracker.roleViewStatuses.push(status);
    }
    if (target.pathname === "/console/events" && method === "POST") {
      tracker.eventStatuses.push(status);
      // 通用页面事件不能冒充角色首页遥测，必须按事件类型单独形成验收计数。
      if (eventType === "dashboard.role_viewed") tracker.roleViewedEventStatuses.push(status);
    }
  });
  // 没有响应的取消/失败请求也必须释放 requestId，避免长时间验收积累无界映射。
  client.on("Network.loadingFailed", ({ requestId }, sessionId) => {
    if (sessionId === pageSession.sessionId) {
      tracker.requestMethods.delete(requestId);
      tracker.requestEventTypes.delete(requestId);
    }
  });
  /** Console 文本只做错误级别和敏感值计数，原始消息不得落盘。 */
  const inspectConsoleText = (text, level) => {
    const value = String(text ?? "");
    // 任一敏感值进入 Console 都直接累计违规，但不复制命中消息。
    if (tracker.secrets.some((secret) => secret && value.includes(secret))) {
      tracker.consolePrivacyViolationCount += 1;
    }
    // error/assert 表示页面存在未处理异常，验收不能只看可见 DOM。
    if (["error", "assert"].includes(String(level))) tracker.consoleErrorCount += 1;
  };
  client.on("Runtime.consoleAPICalled", ({ type, args }, sessionId) => {
    // 忽略其他 target 的 Console，避免 Edge 内部页面噪音改变验收结论。
    if (sessionId !== pageSession.sessionId) return;
    const text = (args ?? []).map((item) => item.value ?? item.description ?? "").join(" ");
    inspectConsoleText(text, type);
  });
  client.on("Runtime.exceptionThrown", (_, sessionId) => {
    // 未捕获页面异常只累计事实，协议异常正文可能含 DOM 或 URL，禁止持久化。
    if (sessionId === pageSession.sessionId) tracker.consoleErrorCount += 1;
  });
  client.on("Log.entryAdded", ({ entry }, sessionId) => {
    // Log 域同样只接受当前页面 Session 的事件。
    if (sessionId !== pageSession.sessionId) return;
    inspectConsoleText(entry?.text, entry?.level);
  });
}


/** 通过真实登录表单建立指定角色 Session，口令只进入页面输入框和登录 POST。 */
async function loginRole(client, sessionId, config, tracker, role) {
  const account = config.accounts[role];
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.login_form");
  const expectedEventCount = tracker.roleViewedEventStatuses.length + 1;
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
    // 三个字段必须同时绑定成功，缺任一输入都不能提交可能歧义的登录请求。
    if (!setValue("tenantSlug", "default") || !setValue("email", values.email)
      || !setValue("password", values.password)) return false;
    form.requestSubmit();
    return true;
  })()`);
  requireCondition(submitted === true, `browser.login.${role}`, "submit");
  const expected = ROLE_EXPECTATIONS[role];
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-role-dashboard-root][data-role-dashboard-view="${expected.selected}"]')
      && document.querySelectorAll('[data-role-dashboard-card]').length === 6`,
    `browser.login.${role}`,
  );
  await waitForTrackerCount(
    () => tracker.roleViewedEventStatuses.length,
    expectedEventCount,
    `browser.login_event.${role}`,
  );
  requireCondition(
    tracker.roleViewedEventStatuses[expectedEventCount - 1] === 200,
    `browser.login_event.${role}`,
    "status",
  );
}


/** 读取当前角色首页的低风险 DOM 合同，不保存用户、租户、指标值或页面正文。 */
async function readRoleDashboardFacts(client, sessionId) {
  return evaluate(client, sessionId, `(() => {
    const root = document.querySelector("[data-role-dashboard-root]");
    const tabs = [...document.querySelectorAll("[data-role-dashboard-view-option]")];
    const cards = [...document.querySelectorAll("[data-role-dashboard-card]")];
    return {
      selected_view: root?.dataset.roleDashboardView || "",
      revision_valid: /^[1-9][0-9]*$/.test(root?.dataset.roleDashboardRevision || ""),
      view_count: tabs.length,
      selected_tab_count: tabs.filter((item) => item.getAttribute("aria-selected") === "true").length,
      card_count: cards.length,
      first_card: cards[0]?.dataset.roleDashboardCard || "",
      heading_ok: (root?.querySelector("h2")?.textContent || "").trim() === "角色首页",
      location_ok: location.origin === "http://localhost:5173"
        && location.pathname === "/" && location.search === "" && location.hash === "#overview",
    };
  })()`);
}


/** 验证一名角色的默认视图、分段数量、首卡片、固定 URL 和双视口布局。 */
async function assertRoleDashboard(
  client,
  sessionId,
  role,
  expected = ROLE_EXPECTATIONS[role],
  stage = `browser.role.${role}`,
) {
  const facts = await readRoleDashboardFacts(client, sessionId);
  requireCondition(facts.selected_view === expected.selected, stage, "selected_view");
  requireCondition(facts.view_count === expected.viewCount, stage, "view_count");
  requireCondition(facts.selected_tab_count === 1, stage, "selected_tab");
  requireCondition(facts.card_count === 6, stage, "card_count");
  requireCondition(facts.first_card === expected.firstCard, stage, "first_card");
  requireCondition(facts.heading_ok && facts.revision_valid && facts.location_ok, stage, "identity");
  const layoutViewports = [];
  for (const viewport of VIEWPORTS) {
    await setViewport(client, sessionId, viewport);
    assertLayoutMetrics(await readLayoutMetrics(client, sessionId), viewport);
    layoutViewports.push(viewport.name);
  }
  // 后续表单交互和截图以桌面视口为基线，不让上一角色的移动视口泄漏到下一流程。
  await setViewport(client, sessionId, VIEWPORTS[0]);
  return Object.freeze({
    role,
    selected_view: facts.selected_view,
    view_count: facts.view_count,
    card_count: facts.card_count,
    first_card: facts.first_card,
    layout_viewports: Object.freeze(layoutViewports),
  });
}


/** 点击 admin 财务分段，并证明切换同步清空旧卡片且最终得到完整新视图。 */
async function switchAdminView(client, sessionId, tracker) {
  const expectedEventCount = tracker.roleViewedEventStatuses.length + 1;
  const transition = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-role-dashboard-view-option="finance"]');
    if (!button) return { clicked: false, cards: -1, busy: false };
    button.click();
    const root = document.querySelector("[data-role-dashboard-root]");
    return {
      clicked: true,
      cards: document.querySelectorAll("[data-role-dashboard-card]").length,
      busy: root?.getAttribute("aria-busy") === "true",
    };
  })()`);
  requireCondition(transition.clicked, "browser.admin_switch", "button_missing");
  requireCondition(
    transition.cards === 0 && transition.busy,
    "browser.admin_switch",
    "old_cards_not_cleared",
  );
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-role-dashboard-root][data-role-dashboard-view="finance"]')
      && document.querySelectorAll('[data-role-dashboard-card]').length === 6`,
    "browser.admin_switch",
  );
  const facts = await readRoleDashboardFacts(client, sessionId);
  requireCondition(facts.selected_view === "finance" && facts.first_card === "cost", "browser.admin_switch", "result");
  await waitForTrackerCount(
    () => tracker.roleViewedEventStatuses.length,
    expectedEventCount,
    "browser.admin_switch_event",
  );
  requireCondition(
    tracker.roleViewedEventStatuses[expectedEventCount - 1] === 200,
    "browser.admin_switch_event",
    "status",
  );
  return Object.freeze({
    selected_view: facts.selected_view,
    old_cards_cleared_immediately: true,
    url_unchanged: facts.location_ok,
  });
}


/**
 * 在发起导航或刷新前预先监听当前 Session 的 load 事件。
 * 先监听后发命令可避免极快本地页面在等待器安装前已完成加载。
 */
async function runAndWaitForPageLoad(client, sessionId, operation, stage, timeoutMs = 30000) {
  let loaded = false;
  const observed = new Promise((resolve) => {
    client.on("Page.loadEventFired", (_, eventSessionId) => {
      // 只接受当前页面 Session 的首次加载，忽略其他 Target 和重复事件。
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


/** 构造不含身份的唯一操作 request_id，仅用于 Control 响应链路核对。 */
function operationRequestId(operation) {
  return `prd065_browser_${operation}_${randomUUID().replaceAll("-", "")}`;
}


/**
 * 调用固定 Control Origin 并校验 HTTP、JSON、request_id 与 trace_id 统一合同。
 * Token 和请求正文只在当前调用栈使用，失败时只抛出固定 stage/code。
 */
async function controlJson(
  config,
  pathname,
  { method = "GET", token = "", payload = null, operation },
  stage,
) {
  const headers = { "X-Request-ID": operationRequestId(operation) };
  // 管理 Token 仅放在当前内存请求头，绝不能拼入 URL、证据或错误正文。
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
    typeof body?.request_id === "string"
      && body.request_id.length > 0
      && body.request_id === response.headers.get("x-request-id"),
    stage,
    "request_id",
  );
  requireCondition(
    typeof body?.trace_id === "string"
      && body.trace_id.length > 0
      && body.trace_id === response.headers.get("x-trace-id"),
    stage,
    "trace_id",
  );
  return body;
}


/** 创建只驻内存的管理员 API Session，专用于撤权与恢复的后端 CAS。 */
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
  requireCondition(token.length >= 16 && body.user?.role === "admin", "browser.permission_admin.login", "identity");
  // 调用方只在撤权事务的 try/finally 内持有 Token，并负责最终注销。
  return token;
}


/** 关闭撤权专项的管理员 API Session，防止验收留下额外有效会话。 */
async function closeAdminControlSession(config, token) {
  const body = await controlJson(config, "/auth/logout", {
    method: "POST",
    token,
    operation: "permission_admin_logout",
  }, "browser.permission_admin.logout");
  requireCondition(body.logged_out === true, "browser.permission_admin.logout", "result");
}


/** 校验权限 code 是无重复的安全枚举，避免把异常响应直接送入 PATCH。 */
function normalizePermissionCodes(value, stage) {
  requireCondition(Array.isArray(value) && value.length > 0, stage, "permission_codes");
  requireCondition(value.every((item) => typeof item === "string"), stage, "permission_codes");
  const codes = [...value];
  requireCondition(
    codes.every((code) => /^[a-z][a-z0-9_.-]{1,80}$/.test(code))
      && new Set(codes).size === codes.length,
    stage,
    "permission_codes",
  );
  return Object.freeze([...codes].sort());
}


/** 从当前租户权限矩阵中精确恢复 finance_admin 的 role_id、revision 和完整集合。 */
async function readFinanceRoleSnapshot(config, token) {
  const body = await controlJson(config, "/permissions", {
    token,
    operation: "permission_matrix_read",
  }, "browser.permission_matrix.read");
  const matches = Array.isArray(body.roles)
    ? body.roles.filter((item) => item?.code === "finance_admin")
    : [];
  requireCondition(matches.length === 1, "browser.permission_matrix.read", "finance_role");
  const role = matches[0];
  const roleId = String(role.role_id ?? "");
  const revision = Number(role.permission_revision);
  requireCondition(/^[A-Za-z0-9_-]{1,120}$/.test(roleId), "browser.permission_matrix.read", "role_id");
  requireCondition(Number.isSafeInteger(revision) && revision >= 1, "browser.permission_matrix.read", "revision");
  return Object.freeze({
    roleId,
    revision,
    permissionCodes: normalizePermissionCodes(role.permission_codes, "browser.permission_matrix.read"),
  });
}


/** 以服务端 revision CAS 完整替换 finance_admin 权限并核对写后权威集合。 */
async function replaceFinanceRoleSnapshot(config, token, snapshot, permissionCodes, operation) {
  const expectedCodes = normalizePermissionCodes(permissionCodes, `browser.permission_matrix.${operation}`);
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
    `browser.permission_matrix.${operation}`,
  );
  requireCondition(body.changed === true && body.role?.code === "finance_admin", `browser.permission_matrix.${operation}`, "changed");
  const updatedCodes = normalizePermissionCodes(
    body.role.permission_codes,
    `browser.permission_matrix.${operation}`,
  );
  requireCondition(
    updatedCodes.length === expectedCodes.length
      && updatedCodes.every((code, index) => code === expectedCodes[index]),
    `browser.permission_matrix.${operation}`,
    "write_projection",
  );
  const revision = Number(body.role.permission_revision);
  requireCondition(
    Number.isSafeInteger(revision) && revision === snapshot.revision + 1,
    `browser.permission_matrix.${operation}`,
    "revision",
  );
  return Object.freeze({
    roleId: snapshot.roleId,
    revision,
    permissionCodes: updatedCodes,
  });
}


/** 比较两个已规范化权限集合，只用于判断 finally 是否需要恢复写。 */
function samePermissionCodes(left, right) {
  return left.length === right.length && left.every((code, index) => code === right[index]);
}


/** 刷新仍处于登录态的角色页，并等待指定视图和该次渲染事件均完成。 */
async function reloadRoleDashboard(client, sessionId, tracker, role, expected, stage) {
  const expectedEventCount = tracker.roleViewedEventStatuses.length + 1;
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.reload", { ignoreCache: true }, sessionId),
    `${stage}.reload`,
  );
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-role-dashboard-root][data-role-dashboard-view="${expected.selected}"]')
      && document.querySelectorAll('[data-role-dashboard-card]').length === 6`,
    stage,
  );
  await waitForTrackerCount(
    () => tracker.roleViewedEventStatuses.length,
    expectedEventCount,
    `${stage}.event`,
  );
  requireCondition(
    tracker.roleViewedEventStatuses[expectedEventCount - 1] === 200,
    `${stage}.event`,
    "status",
  );
  // 只有接口与遥测均成功后才读取 DOM，防止缓存页面制造假通过。
  return assertRoleDashboard(client, sessionId, role, expected, stage);
}


/**
 * 在 finance_admin 浏览器 Session 保持有效时实时撤销必需权限。
 * finally 始终按当前 revision 恢复原集合并注销管理会话，验收失败也不得污染后续环境。
 */
async function exerciseFinancePermissionRevocation(client, sessionId, config, tracker) {
  let adminToken = "";
  let original = null;
  let revokedRole = null;
  try {
    adminToken = await openAdminControlSession(config);
    original = await readFinanceRoleSnapshot(config, adminToken);
    requireCondition(
      original.permissionCodes.includes(FINANCE_VIEW_REVOCATION_PERMISSION),
      "browser.permission_revocation",
      "required_permission_missing",
    );
    const revokedCodes = original.permissionCodes.filter(
      (code) => code !== FINANCE_VIEW_REVOCATION_PERMISSION,
    );
    await replaceFinanceRoleSnapshot(config, adminToken, original, revokedCodes, "revoke");
    revokedRole = await reloadRoleDashboard(
      client,
      sessionId,
      tracker,
      "finance_admin",
      REVOKED_FINANCE_EXPECTATION,
      "browser.permission_revocation.fallback",
    );
  } finally {
    try {
      // 原权限快照存在时必须执行 CAS 恢复，避免失败验收污染共享开发租户。
      if (adminToken && original) {
        const current = await readFinanceRoleSnapshot(config, adminToken);
        // 已被其他流程恢复时跳过写入，避免无意义提升 revision。
        if (!samePermissionCodes(current.permissionCodes, original.permissionCodes)) {
          await replaceFinanceRoleSnapshot(
            config,
            adminToken,
            current,
            original.permissionCodes,
            "restore",
          );
        }
      }
    } finally {
      // 无论权限恢复是否成功都关闭管理 Session，防止遗留高权限会话。
      if (adminToken) await closeAdminControlSession(config, adminToken);
    }
  }
  requireCondition(revokedRole !== null, "browser.permission_revocation", "fallback_missing");
  const restoredRole = await reloadRoleDashboard(
    client,
    sessionId,
    tracker,
    "finance_admin",
    ROLE_EXPECTATIONS.finance_admin,
    "browser.permission_revocation.restored",
  );
  return Object.freeze({
    permission_removed: FINANCE_VIEW_REVOCATION_PERMISSION,
    fallback_view: revokedRole.selected_view,
    fallback_view_count: revokedRole.view_count,
    restored_view: restoredRole.selected_view,
    same_browser_session: true,
    backend_revision_cas: true,
  });
}


/** 设置固定设备指标，并等待两帧保证媒体查询和卡片轨道稳定。 */
async function setViewport(client, sessionId, viewport) {
  await client.send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: viewport.width < 720,
    screenWidth: viewport.width,
    screenHeight: viewport.height,
  }, sessionId);
  await evaluate(client, sessionId, "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
}


/** 读取无正文布局事实，验证页面横向边界、卡片/分段溢出和相互遮挡。 */
async function readLayoutMetrics(client, sessionId) {
  return evaluate(client, sessionId, `(() => {
    const root = document.querySelector("[data-role-dashboard-root]");
    const cards = [...document.querySelectorAll("[data-role-dashboard-card]")];
    const tabs = [...document.querySelectorAll("[data-role-dashboard-view-option]")];
    const rectangles = cards.map((item) => item.getBoundingClientRect());
    let overlapCount = 0;
    for (let left = 0; left < rectangles.length; left += 1) {
      for (let right = left + 1; right < rectangles.length; right += 1) {
        const a = rectangles[left];
        const b = rectangles[right];
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
          && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) overlapCount += 1;
      }
    }
    const rootRect = root?.getBoundingClientRect();
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      root_visible: Boolean(rootRect) && rootRect.width > 0 && rootRect.height > 0,
      root_inside: Boolean(rootRect) && rootRect.left >= -1 && rootRect.right <= innerWidth + 1,
      root_overflow: root ? Math.max(0, root.scrollWidth - root.clientWidth) : 1,
      card_count: cards.length,
      card_overflow_count: cards.filter((item) => item.scrollWidth > item.clientWidth + 1).length,
      tab_overflow_count: tabs.filter((item) => item.scrollWidth > item.clientWidth + 1).length,
      overlap_count: overlapCount,
    };
  })()`);
}


/** 校验固定视口布局，不允许横向滚动、卡片遮挡或动态文本撑宽稳定轨道。 */
function assertLayoutMetrics(metrics, viewport) {
  const stage = `browser.layout.${viewport.name}`;
  requireCondition(
    metrics.viewport_width === viewport.width && metrics.viewport_height === viewport.height,
    stage,
    "viewport",
  );
  requireCondition(
    metrics.document_scroll_width <= viewport.width + 1 && metrics.body_scroll_width <= viewport.width + 1,
    stage,
    "horizontal_overflow",
  );
  requireCondition(metrics.root_visible && metrics.root_inside && metrics.root_overflow <= 1, stage, "root");
  requireCondition(metrics.card_count === 6, stage, "cards");
  requireCondition(metrics.card_overflow_count === 0, stage, "card_overflow");
  requireCondition(metrics.tab_overflow_count === 0, stage, "tab_overflow");
  requireCondition(metrics.overlap_count === 0, stage, "overlap");
  return metrics;
}


/** 捕获当前视口 PNG，隐藏用户 PII 文本后校验尺寸、像素非空和布局。 */
async function captureViewport(client, sessionId, config, viewport) {
  await setViewport(client, sessionId, viewport);
  if (viewport.width < 720) {
    const anchored = await evaluate(client, sessionId, `(() => {
      const root = document.querySelector("[data-role-dashboard-root]");
      if (!root) return false;
      root.scrollIntoView({ block: "start", inline: "nearest" });
      return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))));
    })()`);
    // 移动导航为纵向长列，截图必须锚定 PAGE-092 根节点，不能用只有导航的首屏冒充验收。
    requireCondition(anchored === true, `browser.screenshot.${viewport.name}`, "anchor");
  }
  const layout = assertLayoutMetrics(await readLayoutMetrics(client, sessionId), viewport);
  await evaluate(client, sessionId, `(() => {
    const identity = document.querySelector(".user-menu > div");
    if (identity) identity.style.visibility = "hidden";
    return true;
  })()`);
  const screenshot = await client.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  }, sessionId, 30000);
  await evaluate(client, sessionId, `(() => {
    const identity = document.querySelector(".user-menu > div");
    if (identity) identity.style.visibility = "";
    return true;
  })()`);
  const buffer = Buffer.from(screenshot.data, "base64");
  const filename = `page-092-${viewport.name}.png`;
  await writeFile(path.join(config.artifactDirectory, "screenshots", filename), buffer);
  const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${viewport.name}`);
  return Object.freeze({
    viewport: viewport.name,
    content_anchor: "role_dashboard",
    filename,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    byte_length: buffer.length,
    ...pixels,
    layout,
  });
}


/** 扫描 URL、DOM 与 Storage；认证值只允许留在两个既有 Session 槽位。 */
async function assertBrowserPrivacy(client, sessionId, config, tracker) {
  const supplied = JSON.stringify([config.adminPassword, config.runtimePassword, config.canary]);
  const metrics = await evaluate(client, sessionId, `(() => {
    const secrets = ${supplied};
    const html = document.documentElement.outerHTML;
    const localEntries = Object.entries(localStorage);
    const sessionEntries = Object.entries(sessionStorage);
    const storageText = JSON.stringify([...localEntries, ...sessionEntries]);
    const authValue = sessionStorage.getItem("metahub.auth_token") || "";
    return {
      location_ok: location.origin === "http://localhost:5173"
        && location.pathname === "/" && location.search === "" && location.hash === "#overview",
      local_storage_count: localEntries.length,
      session_key_count: sessionEntries.length,
      unexpected_session_key_count: sessionEntries.filter(([key]) => !${JSON.stringify(AUTH_STORAGE_KEYS)}.includes(key)).length,
      persisted_view_count: (storageText.match(/role[_-]?dashboard|selected[_-]?view|dashboard[_-]?view/gi) || []).length,
      supplied_secret_hit_count: secrets.filter((secret) => secret
        && [location.href, html, storageText].some((value) => value.includes(secret))).length,
      auth_value_outside_slot: Boolean(authValue) && [location.href, html, JSON.stringify(
        [...localEntries, ...sessionEntries.filter(([key]) => key !== "metahub.auth_token")],
      )].some((value) => value.includes(authValue)),
      protected_root_count: document.querySelectorAll("[data-role-dashboard-root]").length,
    };
  })()`);
  requireCondition(metrics.location_ok, "browser.privacy", "location");
  requireCondition(metrics.local_storage_count === 0, "browser.privacy", "local_storage");
  requireCondition(
    metrics.session_key_count === 2 && metrics.unexpected_session_key_count === 0,
    "browser.privacy",
    "session_storage",
  );
  requireCondition(metrics.persisted_view_count === 0, "browser.privacy", "view_persisted");
  requireCondition(metrics.supplied_secret_hit_count === 0, "browser.privacy", "secret_hit");
  requireCondition(!metrics.auth_value_outside_slot, "browser.privacy", "auth_value_outside_slot");
  requireCondition(metrics.protected_root_count === 1, "browser.privacy", "protected_root");
  requireCondition(tracker.externalRequestCount === 0, "browser.privacy", "external_request");
  requireCondition(tracker.forbiddenQueryCount === 0, "browser.privacy", "query");
  requireCondition(tracker.requestPrivacyViolationCount === 0, "browser.privacy", "request");
  requireCondition(tracker.consolePrivacyViolationCount === 0, "browser.privacy", "console");
  requireCondition(tracker.processPrivacyViolationCount === 0, "browser.privacy", "process");
  return Object.freeze({
    url_hits: 0,
    dom_hits: 0,
    storage_hits: 0,
    console_hits: 0,
    request_destination_violations: 0,
    persisted_view_hits: 0,
  });
}


/** 通过真实按钮注销，并证明 Session 槽位和受保护角色首页同步销毁。 */
async function logoutRole(client, sessionId, role) {
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector("#logout-button");
    if (!button) return false;
    button.click();
    return true;
  })()`);
  requireCondition(clicked, `browser.logout.${role}`, "button_missing");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `browser.logout.${role}`);
  const cleared = await evaluate(client, sessionId, `(() => ({
    auth_slots: ${JSON.stringify(AUTH_STORAGE_KEYS)}.filter((key) => sessionStorage.getItem(key) !== null).length,
    protected_roots: document.querySelectorAll("[data-role-dashboard-root]").length,
  }))()`);
  requireCondition(cleared.auth_slots === 0, `browser.logout.${role}`, "auth_slots");
  requireCondition(cleared.protected_roots === 0, `browser.logout.${role}`, "protected_dom");
}


/** 校验所有 role-view query 都是空 query 或单一合法 view，不携带身份字段。 */
function assertRoleViewQueries(tracker) {
  // 六角色默认视图、admin 切换、finance 撤权回落与恢复各至少产生一次查询。
  requireCondition(tracker.roleViewQueries.length >= 9, "browser.network", "role_view_missing");
  for (const entries of tracker.roleViewQueries) {
    requireCondition(entries.length <= 1, "browser.network", "query_count");
    if (entries.length === 1) {
      requireCondition(
        entries[0][0] === "view"
          && ["executive", "finance", "security", "developer", "operations"].includes(entries[0][1]),
        "browser.network",
        "query_shape",
      );
    }
  }
}


/** 递归删除本批次 profile/cache/crash/browser-home 前再次验证物理路径，保留截图和 JSON 证据。 */
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
        // Edge 子进程退出后 Windows 句柄可能短暂延迟释放，只在本批次目录内做有界重试。
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    }
    // 任一浏览器状态目录仍存在都表示清理未完成，不能用成功证据掩盖残留。
    requireCondition(removed && !existsSync(target), `cleanup.${name}`, "remove_failed");
  }
}


/** 等待 Edge 正常退出；超时后只终止本脚本创建且仍绑定的子进程。 */
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


/** 写入脱敏 JSON 前复核所有内存凭据、账号邮箱和 canary 均未进入证据。 */
async function publishEvidence(config, evidence) {
  const serialized = `${JSON.stringify(evidence, null, 2)}\n`;
  const forbiddenValues = [
    config.adminPassword,
    config.runtimePassword,
    config.canary,
    ...Object.values(config.accounts).map((item) => item.email),
  ];
  requireCondition(
    forbiddenValues.every((value) => !value || !serialized.includes(value)),
    "evidence",
    "sensitive_value",
  );
  await writeFile(path.join(config.artifactDirectory, "browser-evidence.json"), serialized, "utf8");
}


/** 执行六角色、admin 切换、双视口、事件、隐私和注销真实浏览器验收。 */
async function executeAcceptance(config) {
  const tracker = createTracker(config);
  let child = null;
  let client = null;
  let pageSession = null;
  try {
    child = launchEdge(config, tracker);
    const webSocketUrl = await waitForDebugger(config.debugPort);
    client = new CdpClient(webSocketUrl);
    // 必须先完成 Browser WebSocket 握手再发送 Target 命令；未连接 client 会错误报告 not_open。
    await client.connect();
    pageSession = await createPageSession(client);
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
    await waitForDom(client, pageSession.sessionId, "document.querySelector('#login-form')", "browser.initial_page");

    const roles = [];
    await loginRole(client, pageSession.sessionId, config, tracker, "admin");
    roles.push(await assertRoleDashboard(client, pageSession.sessionId, "admin"));
    const adminSwitch = await switchAdminView(client, pageSession.sessionId, tracker);
    const screenshots = [];
    for (const viewport of VIEWPORTS) {
      screenshots.push(await captureViewport(client, pageSession.sessionId, config, viewport));
    }
    const privacy = await assertBrowserPrivacy(client, pageSession.sessionId, config, tracker);
    await setViewport(client, pageSession.sessionId, VIEWPORTS[0]);
    await logoutRole(client, pageSession.sessionId, "admin");

    let permissionRevocation = null;
    for (const role of ["developer", "viewer", "finance_admin", "security_admin", "ops_admin"]) {
      await loginRole(client, pageSession.sessionId, config, tracker, role);
      roles.push(await assertRoleDashboard(client, pageSession.sessionId, role));
      // 财务角色额外覆盖实时撤权、默认视图回落和原权限恢复的完整链路。
      if (role === "finance_admin") {
        permissionRevocation = await exerciseFinancePermissionRevocation(
          client,
          pageSession.sessionId,
          config,
          tracker,
        );
      }
      await logoutRole(client, pageSession.sessionId, role);
    }

    assertRoleViewQueries(tracker);
    requireCondition(roles.length === 6, "browser.roles", "count");
    requireCondition(permissionRevocation !== null, "browser.permission_revocation", "missing");
    requireCondition(tracker.loginStatuses.filter((status) => status === 200).length >= 6, "browser.network", "login_status");
    requireCondition(tracker.logoutStatuses.filter((status) => status === 200).length >= 6, "browser.network", "logout_status");
    requireCondition(tracker.navigationStatuses.filter((status) => status === 200).length >= 6, "browser.network", "navigation_status");
    requireCondition(
      tracker.roleViewStatuses.length >= 9
        && tracker.roleViewStatuses.every((status) => status === 200),
      "browser.network",
      "role_view_status",
    );
    requireCondition(
      tracker.roleViewedEventStatuses.length >= 9
        && tracker.roleViewedEventStatuses.every((status) => status === 200),
      "browser.network",
      "role_view_event_status",
    );
    requireCondition(
      tracker.eventStatuses.length >= 9 && tracker.eventStatuses.every((status) => status === 200),
      "browser.network",
      "event_status",
    );
    requireCondition(tracker.consoleErrorCount === 0, "browser.console", "error");
    requireCondition(tracker.edgeProcessErrorCount === 0, "browser.edge", "process_error");
    requireCondition(screenshots.every((item) => item.non_blank_pixels), "browser.screenshots", "blank");

    const evidence = Object.freeze({
      prd_id: "PRD-065",
      page_id: "PAGE-092",
      browser_family: "Microsoft Edge",
      generated_at: new Date().toISOString(),
      checks: Object.freeze({
        six_role_defaults: true,
        admin_view_switch: true,
        old_cards_cleared_immediately: true,
        realtime_permission_revocation: true,
        permission_restoration: true,
        backend_role_view_200: true,
        rendered_event_200: true,
        logout_cleanup: true,
        privacy,
      }),
      roles,
      admin_switch: adminSwitch,
      permission_revocation: permissionRevocation,
      network: Object.freeze({
        login_200_count: tracker.loginStatuses.filter((status) => status === 200).length,
        logout_200_count: tracker.logoutStatuses.filter((status) => status === 200).length,
        navigation_200_count: tracker.navigationStatuses.filter((status) => status === 200).length,
        role_view_200_count: tracker.roleViewStatuses.filter((status) => status === 200).length,
        role_viewed_event_200_count: tracker.roleViewedEventStatuses.filter((status) => status === 200).length,
        event_200_count: tracker.eventStatuses.filter((status) => status === 200).length,
      }),
      screenshots,
    });
    await publishEvidence(config, evidence);
    return evidence;
  } finally {
    if (client) {
      // Browser.close 可能先关闭 WebSocket，因此清理阶段只要求进程最终退出，不覆盖原验收结论。
      try {
        await client.send("Browser.close");
      } catch {
        // send 在连接已关闭时会同步失败；这里必须吞掉清理错误，保留真正的验收失败阶段。
      }
      client.close();
    }
    if (child) await waitForChildExit(child);
    await removeSensitiveBrowserState(config);
  }
}


/** CLI 入口只输出固定成功摘要或固定失败分类，不输出凭据、URL、DOM 或原始异常。 */
async function main() {
  try {
    const config = await readConfiguration();
    const evidence = await runBrowserOperation("browser.acceptance", () => executeAcceptance(config));
    process.stdout.write(`${JSON.stringify({
      status: "passed",
      prd_id: evidence.prd_id,
      page_id: evidence.page_id,
      role_count: evidence.roles.length,
      screenshot_count: evidence.screenshots.length,
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
  ROLE_EXPECTATIONS,
  VIEWPORTS,
  assertLayoutMetrics,
  consumeTrackedRequestMethod,
  normalizeLoopbackOrigin,
  normalizePermissionCodes,
  resolveArtifactDirectory,
  samePermissionCodes,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
