/**
 * PRD-061 PAGE-088 Provider 状态页真实浏览器验收器。
 *
 * 安全边界：只访问三个固定本机 loopback Origin；浏览器运行目录、截图和脱敏 JSON 证据只能写入
 * `D:/MetaHub/.artifacts/prd061-browser/<run-id>` 的全新普通子目录。脚本不记录 Token、密码、
 * Provider 名称、Endpoint、维护备注、故障标识或筛选值，也不会覆盖既有证据。
 */

import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { lstat, mkdir, realpath, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";

/** CommonJS 桥只用于加载桌面运行时提供的 Playwright，不把它加入 Console 生产依赖。 */
const require = createRequire(import.meta.url);
/** 脚本位置决定仓库根，调用方不能通过当前工作目录把证据根重定向到仓库外。 */
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
/** PRD-061 浏览器证据唯一允许的父目录。 */
const ARTIFACT_ROOT = path.join(REPOSITORY_ROOT, ".artifacts", "prd061-browser");
/** 登录期间唯一允许保存认证数据的 Session Storage 槽位。 */
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);
/** 页面必须覆盖的冻结桌面与移动视口；字体大小不随视口动态缩放。 */
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
/** Provider 类型必须来自页面和后端共同冻结的白名单，不能把响应文本直接拼入查询。 */
const PROVIDER_TYPES = new Set([
  "zhipu",
  "dashscope",
  "wenxin",
  "volcengine",
  "hunyuan",
  "deepseek",
  "openai_compatible",
  "custom",
  "migration_compatible",
]);
/** 证据对象禁止出现的敏感字段名；即使值已脱敏也不保留易误用的业务槽位。 */
const FORBIDDEN_EVIDENCE_KEY = /password|token|secret|prompt|response|authorization|cookie|provider[_-]?key|maintenance[_-]?note|base[_-]?url|endpoint|filter/i;
/** 浏览器 URL 中禁止出现的敏感查询键，避免同源请求把凭据写入历史或代理日志。 */
const FORBIDDEN_QUERY_KEY = /password|token|secret|prompt|response|authorization|cookie|provider[_-]?key|tenant[_-]?id/i;


/** 使用固定 stage/code 表达失败，避免原始异常、URL、DOM 或响应正文进入终端和流水线日志。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存不含业务数据的稳定失败分类。 */
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 失败关闭断言；stage/code 必须由源码固定，不能拼接外部输入。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** 把浏览器 API 异常折叠成固定分类，禁止 Playwright 错误携带页面正文或完整 URL。 */
async function runBrowserOperation(stage, operation) {
  try {
    return await operation();
  } catch (error) {
    // 已脱敏的验收失败必须保留原分类；其他异常统一折叠，避免原始页面或 URL 进入日志。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "operation_failed");
  }
}


/** 为内部一次性信号增加固定超时，避免路由未到达时验收进程永久占用服务和证据目录。 */
async function waitForSignal(signal, stage, timeoutMs = 30000) {
  let timer = null;
  try {
    return await Promise.race([
      signal,
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new BrowserAcceptanceFailure(stage, "timeout")),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}


/** 归一化固定 localhost 服务 Origin，并拒绝别名、凭据、路径、查询、Hash、HTTPS 或非预期端口。 */
function normalizeLoopbackOrigin(value, expectedPort, stage = "config.origin") {
  let parsed;
  try {
    parsed = new URL(String(value));
  } catch {
    throw new BrowserAcceptanceFailure(stage, "url_invalid");
  }
  requireCondition(parsed.protocol === "http:", stage, "protocol");
  // Console 生产入口把 Control/Gateway 固定为 localhost；验收配置也必须使用同一 Origin，
  // 否则 127.0.0.1 页面会把真实 localhost API 误判成外部请求并形成不可复现的假失败。
  requireCondition(parsed.hostname === "localhost", stage, "host");
  requireCondition(parsed.port === String(expectedPort), stage, "port");
  requireCondition(!parsed.username && !parsed.password, stage, "credentials");
  requireCondition(parsed.pathname === "/" && !parsed.search && !parsed.hash, stage, "shape");
  return parsed.origin;
}


/**
 * 解析调用方指定的证据目录。
 * 只接受固定证据根的一级 run-id，拒绝父目录、证据根本身、嵌套目录和路径穿越。
 */
function resolveArtifactDirectory(value, repositoryRoot = REPOSITORY_ROOT) {
  const root = path.resolve(repositoryRoot, ".artifacts", "prd061-browser");
  const target = path.resolve(String(value || ""));
  const relative = path.relative(root, target);
  requireCondition(
    /^[A-Za-z0-9][A-Za-z0-9._-]{2,80}$/.test(relative)
      && !relative.includes(path.sep)
      && !path.isAbsolute(relative),
    "config.artifact_directory",
    "outside_or_nested",
  );
  return target;
}


/** 校验已有目录是未经过链接跳转的普通目录，防止 reparse point 把写入导向仓库外。 */
async function assertPlainDirectory(directory, stage) {
  let information;
  let canonical;
  try {
    information = await lstat(directory);
    canonical = await realpath(directory);
  } catch {
    throw new BrowserAcceptanceFailure(stage, "missing_or_unreadable");
  }
  requireCondition(
    information.isDirectory() && !information.isSymbolicLink(),
    stage,
    "not_plain_directory",
  );
  requireCondition(
    path.normalize(canonical).toLowerCase() === path.normalize(directory).toLowerCase(),
    stage,
    "reparse_point",
  );
  return directory;
}


/** 独占创建全新证据目录及固定子目录；任一已存在目标都失败，绝不复用或覆盖历史证据。 */
async function createArtifactDirectory(value) {
  const target = resolveArtifactDirectory(value);
  const artifacts = path.dirname(ARTIFACT_ROOT);
  await assertPlainDirectory(artifacts, "config.artifacts_root");
  if (!existsSync(ARTIFACT_ROOT)) {
    try {
      await mkdir(ARTIFACT_ROOT, { recursive: false });
    } catch {
      throw new BrowserAcceptanceFailure("config.artifact_root", "create_failed");
    }
  }
  await assertPlainDirectory(ARTIFACT_ROOT, "config.artifact_root");
  requireCondition(!existsSync(target), "config.artifact_directory", "already_exists");
  try {
    await mkdir(target, { recursive: false });
    await assertPlainDirectory(target, "config.artifact_directory");
    await mkdir(path.join(target, "screenshots"), { recursive: false });
    await mkdir(path.join(target, "downloads"), { recursive: false });
    await mkdir(path.join(target, "browser-home"), { recursive: false });
    await mkdir(path.join(target, "browser-home", "local"), { recursive: false });
    await mkdir(path.join(target, "browser-home", "roaming"), { recursive: false });
  } catch (error) {
    // 固定失败对象不应再次改写，其他文件系统异常则统一转换成不含物理路径的错误码。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure("config.artifact_directory", "create_failed");
  }
  return target;
}


/** 读取本地地址、浏览器、管理员身份和证据目录；敏感配置只停留在进程内存。 */
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
  const gatewayOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_GATEWAY_URL || "http://localhost:8000",
    8000,
    "config.gateway",
  );
  requireCondition(
    new Set([consoleOrigin, controlOrigin, gatewayOrigin]).size === 3,
    "config.origins",
    "duplicate",
  );
  const chromePath = String(environment.METAHUB_PLAYWRIGHT_CHROME_PATH || "");
  const adminPassword = String(environment.METAHUB_ACCEPTANCE_ADMIN_PASSWORD || "");
  requireCondition(path.isAbsolute(chromePath) && existsSync(chromePath), "config.chrome", "missing");
  requireCondition(adminPassword.length >= 12, "config.identity", "password_missing");
  const artifactDirectory = await createArtifactDirectory(
    environment.METAHUB_ACCEPTANCE_ARTIFACT_DIR,
  );
  return Object.freeze({
    artifactDirectory,
    consoleOrigin,
    controlOrigin,
    gatewayOrigin,
    chromePath,
    tenantSlug: String(environment.METAHUB_ACCEPTANCE_TENANT || "default"),
    adminEmail: String(environment.METAHUB_ACCEPTANCE_EMAIL || "admin@metahub.local"),
    adminPassword,
    sensitiveCanary: String(environment.METAHUB_ACCEPTANCE_SENSITIVE_CANARY || ""),
  });
}


/** 判断请求是否严格位于允许的本机 Origin，且 URL 不携带敏感查询参数或 URL 凭据。 */
function browserRequestAllowed(value, allowedOrigins) {
  try {
    const parsed = new URL(String(value));
    if (["data:", "blob:", "about:"].includes(parsed.protocol)) return true;
    if (!allowedOrigins.has(parsed.origin) || parsed.username || parsed.password) return false;
    return [...parsed.searchParams.keys()].every((key) => !FORBIDDEN_QUERY_KEY.test(key));
  } catch {
    return false;
  }
}


/** 判断 URL 是否恰好包含给定唯一查询参数，拒绝重复键、漏项和任何额外字段。 */
function hasExactSearchParams(target, expected) {
  const expectedEntries = Object.entries(expected).map(([key, value]) => [key, String(value)]);
  const actualEntries = [...target.searchParams.entries()];
  if (actualEntries.length !== expectedEntries.length) return false;
  return expectedEntries.every(([key, value]) => (
    target.searchParams.getAll(key).length === 1 && target.searchParams.get(key) === value
  ));
}


/** 校验立即探测请求为同源无查询 POST、空 JSON 对象和 JSON Content-Type。 */
function isExactProbeRequest(request, controlOrigin) {
  let target;
  try {
    target = new URL(request.url());
  } catch {
    return false;
  }
  const contentType = String(request.headers()?.["content-type"] || "").split(";", 1)[0].trim().toLowerCase();
  return target.origin === controlOrigin
    && /^\/providers\/[^/]+\/probe$/.test(target.pathname)
    && target.search === ""
    && request.method() === "POST"
    && request.postData() === "{}"
    && contentType === "application/json";
}


/**
 * 构造浏览器子进程环境。
 * 仅继承 Windows 启动必需的非敏感变量，再把所有用户和临时目录固定到当前证据批次，
 * 防止浏览器读取宿主机凭据或写到系统目录。使用 allowlist 是为了覆盖 PAT、Credential、Access Key
 * 等无法由有限敏感词 blocklist 穷举的环境变量名称。
 */
function buildBrowserEnvironment(environment, artifactDirectory) {
  const sanitized = {};
  const requiredName = /^(?:path|pathext|systemroot|windir|comspec|os|number_of_processors|processor_(?:architecture|identifier)|lang|lc_[a-z_]+)$/i;
  for (const [key, value] of Object.entries(environment)) {
    // 浏览器只继承启动所需 allowlist；其他业务配置和未知凭据变量一律不传入子进程。
    if (!requiredName.test(key)) continue;
    sanitized[key] = value;
  }
  const browserHome = path.join(artifactDirectory, "browser-home");
  sanitized.TEMP = artifactDirectory;
  sanitized.TMP = artifactDirectory;
  sanitized.HOME = browserHome;
  sanitized.USERPROFILE = browserHome;
  sanitized.LOCALAPPDATA = path.join(browserHome, "local");
  sanitized.APPDATA = path.join(browserHome, "roaming");
  return sanitized;
}


/** 校验 status.viewed 的精确白名单合同；多余 tenant、Provider、筛选或 Token 字段都会失败。 */
function isExactStatusViewedEvent(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  if (JSON.stringify(Object.keys(payload).sort()) !== JSON.stringify([
    "event_type",
    "navigation_item_id",
    "path",
    "payload",
    "target_section",
  ])) return false;
  const detail = payload.payload;
  return payload.event_type === "status.viewed"
    && payload.navigation_item_id === "provider-health"
    && payload.target_section === "provider-health"
    && payload.path === "/#provider-health"
    && detail
    && typeof detail === "object"
    && !Array.isArray(detail)
    && JSON.stringify(Object.keys(detail).sort()) === JSON.stringify(["hash", "source", "viewport_class"])
    // 本验收流程从桌面侧栏进入 PAGE-088；来源或视口不匹配说明事件并非本次真实操作。
    && detail.source === "sidebar"
    && detail.hash === "#provider-health"
    && detail.viewport_class === "desktop";
}


/** 校验真实状态响应具备分页、汇总、三类 P95、调用/探测计数和故障数组，不复制业务正文进证据。 */
function validateProviderStatusPayload(payload, { requireProvider = true } = {}) {
  requireCondition(
    payload && typeof payload === "object" && !Array.isArray(payload),
    "browser.status_payload",
    "object",
  );
  requireCondition(Array.isArray(payload.provider_statuses), "browser.status_payload", "list");
  requireCondition(
    payload.summary && typeof payload.summary === "object" && !Array.isArray(payload.summary),
    "browser.status_payload",
    "summary",
  );
  for (const key of ["total", "limit", "offset"]) {
    requireCondition(
      Number.isSafeInteger(payload[key]) && payload[key] >= 0,
      "browser.status_payload",
      "pagination",
    );
  }
  requireCondition(
    !requireProvider || payload.provider_statuses.length > 0,
    "browser.status_payload",
    "provider_missing",
  );
  let filterType = "";
  for (const item of payload.provider_statuses) {
    requireCondition(
      item && typeof item === "object" && !Array.isArray(item),
      "browser.status_payload",
      "item",
    );
    requireCondition(
      typeof item.provider_id === "string" && item.provider_id.length > 0,
      "browser.status_payload",
      "provider_id",
    );
    requireCondition(Array.isArray(item.recent_failures), "browser.status_payload", "failures");
    const countKeys = [
      "terminal_call_count",
      "eligible_call_count",
      "success_count",
      "provider_failure_count",
      "credential_failure_count",
      "excluded_call_count",
      "probe_total",
      "probe_success_count",
      "probe_failure_count",
      "probe_inconclusive_count",
      "call_latency_sample_count",
      "stream_first_chunk_sample_count",
      "probe_latency_sample_count",
    ];
    for (const key of countKeys) {
      requireCondition(
        Number.isSafeInteger(item[key]) && item[key] >= 0,
        "browser.status_payload",
        "metrics",
      );
    }
    // 六项调用计数和四项探测计数必须可重算，避免页面把缺失/矛盾事实渲染成看似正常的横杠。
    requireCondition(
      item.eligible_call_count === item.success_count + item.provider_failure_count
        && item.terminal_call_count === item.eligible_call_count + item.excluded_call_count
        && item.credential_failure_count <= item.excluded_call_count,
      "browser.status_payload",
      "call_invariant",
    );
    requireCondition(
      item.probe_total === item.probe_success_count
        + item.probe_failure_count
        + item.probe_inconclusive_count,
      "browser.status_payload",
      "probe_invariant",
    );
    assertRatioField(
      item.success_rate_bps,
      item.success_count,
      item.eligible_call_count,
      "call_rate",
    );
    assertRatioField(
      item.probe_success_rate_bps,
      item.probe_success_count,
      item.probe_success_count + item.probe_failure_count,
      "probe_rate",
    );
    for (const [sampleKey, latencyKey] of [
      ["call_latency_sample_count", "call_latency_p95_ms"],
      ["stream_first_chunk_sample_count", "stream_first_chunk_p95_ms"],
      ["probe_latency_sample_count", "probe_latency_p95_ms"],
    ]) {
      assertLatencyField(item[latencyKey], item[sampleKey], latencyKey);
    }
    // 只选择冻结白名单中的首个类型用于 UI 筛选，不能把任意后端文本重新发送到查询串。
    if (!filterType && PROVIDER_TYPES.has(item.provider_type)) filterType = item.provider_type;
  }
  // 真实成功态必须提供至少一个可安全筛选的类型，否则筛选验收会退化成伪操作。
  if (requireProvider) {
    requireCondition(Boolean(filterType), "browser.status_payload", "filter_type");
  }
  return Object.freeze({ provider_present: payload.provider_statuses.length > 0, filterType });
}


/** 校验万分比字段与整数 half-up 口径一致；BigInt 避免大计数相乘后丢失精度。 */
function assertRatioField(value, numerator, denominator, code) {
  if (denominator === 0) {
    requireCondition(value === null, "browser.status_payload", code);
    return;
  }
  const expected = Number(
    (BigInt(numerator) * 10_000n + BigInt(denominator) / 2n) / BigInt(denominator),
  );
  requireCondition(
    Number.isSafeInteger(value) && value === expected && value >= 0 && value <= 10_000,
    "browser.status_payload",
    code,
  );
}


/** 校验 P95 与样本数的空值合同：零样本必须 null，非零样本必须是非负安全整数。 */
function assertLatencyField(value, sampleCount, code) {
  requireCondition(
    sampleCount === 0
      ? value === null
      : Number.isSafeInteger(value) && value >= 0,
    "browser.status_payload",
    code,
  );
}


/** 构造不含 Provider 数据的固定空态响应；可选 canary 作为未知字段验证前端不会展示或持久化它。 */
function emptyStatusPayload(sensitiveCanary = "") {
  const payload = {
    provider_statuses: [],
    summary: { total: 0, healthy: 0, unhealthy: 0, observing: 0, unknown: 0, disabled: 0 },
    total: 0,
    limit: 50,
    offset: 0,
    window_started_at: "2026-08-01T00:00:00Z",
    window_ended_at: "2026-08-02T00:00:00Z",
    request_id: "req_prd061_browser_empty",
    trace_id: "trc_prd061_browser_empty",
  };
  // Canary 只进入内存中的模拟响应，绝不进入 DOM、Storage、截图摘要或最终 JSON 证据。
  if (sensitiveCanary) payload.api_key = sensitiveCanary;
  return payload;
}


/** 创建受控状态接口场景；loading 只延迟一次真实请求，错误和空态不读取或保存真实正文。 */
function createStatusScenario(sensitiveCanary = "") {
  let mode = "real";
  let holdArmed = false;
  let heldRoute = null;
  let heldResolve = null;

  return {
    /** 为下一条状态 GET 建立一次性延迟，并返回其到达通知。 */
    armLoadingHold() {
      requireCondition(!holdArmed && !heldRoute, "browser.loading", "hold_busy");
      holdArmed = true;
      return new Promise((resolve) => { heldResolve = resolve; });
    },

    /** 切换固定模拟场景；任意外部文本均不能成为响应模式。 */
    setMode(nextMode) {
      requireCondition(["real", "error", "empty"].includes(nextMode), "browser.scenario", "mode");
      mode = nextMode;
    },

    /** 路由层处理状态 GET；返回 true 表示已接管请求。 */
    async handle(route) {
      if (holdArmed) {
        holdArmed = false;
        heldRoute = route;
        heldResolve?.();
        heldResolve = null;
        return true;
      }
      // 错误态只返回统一错误码和固定 request_id，不回放真实异常正文或上游信息。
      if (mode === "error") {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          headers: {
            "Cache-Control": "no-store",
            "X-Request-ID": "req_prd061_browser_503",
            "X-Trace-ID": "trc_prd061_browser_503",
          },
          body: JSON.stringify({
            code: "provider_status.query_unavailable",
            message: "Provider 状态暂不可用",
            request_id: "req_prd061_browser_503",
            trace_id: "trc_prd061_browser_503",
          }),
        });
        return true;
      }
      if (mode === "empty") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: { "Cache-Control": "no-store" },
          body: JSON.stringify(emptyStatusPayload(sensitiveCanary)),
        });
        return true;
      }
      return false;
    },

    /** 放行已捕获的真实状态 GET；未捕获请求时失败关闭，避免 loading 成为伪截图。 */
    async releaseLoadingHold() {
      requireCondition(Boolean(heldRoute), "browser.loading", "request_missing");
      const route = heldRoute;
      heldRoute = null;
      await route.continue();
    },
  };
}


/** 对浏览器 console error 分类；唯一可忽略项是验收器主动制造的 503 资源提示。 */
function classifyBrowserConsoleMessage({ type, text, locationUrl = "" }) {
  if (type !== "error") return "ignored";
  const expected503 = /Failed to load resource/i.test(String(text))
    && /503/.test(String(text))
    // location 缺失时无法证明 503 来自验收器主动模拟的状态接口，必须按未知错误失败关闭。
    && Boolean(locationUrl)
    && /\/status\/providers(?:\?|$)/.test(String(locationUrl));
  return expected503 ? "expected_http_503" : "unexpected_error";
}


/** 安装外部网络门禁以及脱敏计数器；事件正文只做布尔合同判断，绝不写入 tracker。 */
async function installBrowserGuards(context, page, config, scenario) {
  const allowedOrigins = new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]);
  const tracker = {
    blockedRequestCount: 0,
    pageErrorCount: 0,
    consoleErrorCount: 0,
    expectedHttpConsoleErrorCount: 0,
    statusGetCount: 0,
    statusFilterRequestCount: 0,
    statusResetRequestCount: 0,
    probePostCount: 0,
    statusViewedCount: 0,
    statusViewedExactCount: 0,
    loginStatuses: [],
    meStatuses: [],
    navigationStatuses: [],
    statusStatuses: [],
    probeStatuses: [],
    eventStatuses: [],
    logoutStatuses: [],
  };
  page.on("pageerror", () => { tracker.pageErrorCount += 1; });
  page.on("console", (message) => {
    const classification = classifyBrowserConsoleMessage({
      type: message.type(),
      text: message.text(),
      locationUrl: message.location()?.url || "",
    });
    // 主动制造的 503 单独计数，避免把预期异常流误判为页面脚本错误。
    if (classification === "expected_http_503") tracker.expectedHttpConsoleErrorCount += 1;
    // 其余 console error 全部失败关闭，但正文不进入证据或终端输出。
    if (classification === "unexpected_error") tracker.consoleErrorCount += 1;
  });
  page.on("response", (response) => {
    try {
      const target = new URL(response.url());
      if (target.origin !== config.controlOrigin) return;
      const method = response.request().method();
      // 只记录登录整数状态码，不保留认证响应正文或 Header。
      if (target.pathname === "/auth/login" && method === "POST") tracker.loginStatuses.push(response.status());
      // 登录后的身份与导航是 shell 出现的必要条件；仅保存整数状态用于失败归因。
      if (target.pathname === "/auth/me" && method === "GET") tracker.meStatuses.push(response.status());
      if (target.pathname === "/console/navigation" && method === "GET") {
        tracker.navigationStatuses.push(response.status());
      }
      // 状态页只累计整数状态码，Provider 数据和筛选值不进入 tracker。
      if (target.pathname === "/status/providers" && method === "GET") tracker.statusStatuses.push(response.status());
      // 探测响应同样只累计状态码，路径中的 Provider 标识不会被复制。
      if (/^\/providers\/[^/]+\/probe$/.test(target.pathname) && method === "POST") tracker.probeStatuses.push(response.status());
      // 访问事件只保留 HTTP 结果；精确正文合同在请求路由中转成布尔计数。
      if (target.pathname === "/console/events" && method === "POST") tracker.eventStatuses.push(response.status());
      // 注销响应仅用于确认后端销毁完成，不保存 Session 标识或 Cookie。
      if (target.pathname === "/auth/logout" && method === "POST") tracker.logoutStatuses.push(response.status());
    } catch {
      // 非法 URL 已由总路由门禁拒绝；响应监听器只累计已规范化的本机状态码。
    }
  });
  await context.route("**/*", async (route) => {
    const request = route.request();
    if (!browserRequestAllowed(request.url(), allowedOrigins)) {
      tracker.blockedRequestCount += 1;
      await route.abort("blockedbyclient");
      return;
    }
    let target;
    try {
      target = new URL(request.url());
    } catch {
      tracker.blockedRequestCount += 1;
      await route.abort("blockedbyclient");
      return;
    }
    if (
      target.origin === config.controlOrigin
      && target.pathname === "/status/providers"
      && request.method() === "GET"
    ) {
      tracker.statusGetCount += 1;
      // 筛选计数只记录“是否带类型键”，真实类型值禁止进入证据状态。
      if (target.searchParams.has("provider_type")) tracker.statusFilterRequestCount += 1;
      // 已发生筛选后出现不带类型键的请求才视为重置候选，避免初始加载误计。
      if (!target.searchParams.has("provider_type") && tracker.statusFilterRequestCount > 0) {
        tracker.statusResetRequestCount += 1;
      }
      // 模拟器接管后不能再次 continue，同一请求只能由一个路由分支完成。
      if (await scenario.handle(route)) return;
    }
    if (
      target.origin === config.controlOrigin
      && target.pathname === "/console/events"
      && request.method() === "POST"
    ) {
      let payload = null;
      try { payload = request.postDataJSON(); } catch { payload = null; }
      if (payload?.event_type === "status.viewed") {
        tracker.statusViewedCount += 1;
        if (isExactStatusViewedEvent(payload)) tracker.statusViewedExactCount += 1;
      }
    }
    if (
      target.origin === config.controlOrigin
      && /^\/providers\/[^/]+\/probe$/.test(target.pathname)
      && request.method() === "POST"
    ) {
      tracker.probePostCount += 1;
    }
    await route.continue();
  });
  return tracker;
}


/** 使用真实登录表单建立管理员 Session；密码仅传给输入框，不读取或输出响应 Token。 */
async function loginAdministrator(page, config, tracker) {
  await runBrowserOperation(
    "browser.login.navigate",
    () => page.goto(`${config.consoleOrigin}/`, { waitUntil: "domcontentloaded" }),
  );
  const form = page.locator("#login-form");
  await runBrowserOperation(
    "browser.login.form",
    () => form.waitFor({ state: "visible", timeout: 30000 }),
  );
  await runBrowserOperation("browser.login.fill", async () => {
    await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
    await form.locator('[name="email"]').fill(config.adminEmail);
    await form.locator('[name="password"]').fill(config.adminPassword);
  });
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/auth/login"
        && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await runBrowserOperation(
    "browser.login.submit",
    () => form.locator('button[type="submit"]').click(),
  );
  const response = await runBrowserOperation("browser.login.response", () => responsePromise);
  requireCondition(response.status() === 200, "browser.login", "status");
  try {
    // 顶栏与 console-shell 是兄弟节点，必须分别等待；后代选择器会把真实成功登录误判成超时。
    await Promise.all([
      page.locator("#logout-button").waitFor({ state: "visible", timeout: 30000 }),
      page.locator(".console-shell").waitFor({ state: "visible", timeout: 30000 }),
    ]);
  } catch {
    const state = await runBrowserOperation("browser.login.shell.inspect", () => page.evaluate(() => ({
      login_form_present: Boolean(document.querySelector("#login-form")),
      auth_token_present: Boolean(sessionStorage.getItem("metahub.auth_token")),
      auth_context_present: Boolean(sessionStorage.getItem("metahub.auth_context")),
    })));
    // 归因只使用布尔和 HTTP 整数，不输出 Session、DOM、Console 正文或底层异常。
    if (tracker.pageErrorCount > 0) throw new BrowserAcceptanceFailure("browser.login.shell", "page_error");
    if (tracker.consoleErrorCount > 0) throw new BrowserAcceptanceFailure("browser.login.shell", "console_error");
    if (!state.auth_token_present) throw new BrowserAcceptanceFailure("browser.login.shell", "session_not_stored");
    // Token 存在但身份快照或 /auth/me 200 缺失时，不能把残缺登录态当成可验收 shell。
    if (!state.auth_context_present || !tracker.meStatuses.includes(200)) {
      // 只返回固定分类，禁止把身份响应、Session 或页面错误正文带入终端。
      throw new BrowserAcceptanceFailure("browser.login.shell", "auth_context_unavailable");
    }
    if (!tracker.navigationStatuses.includes(200)) {
      throw new BrowserAcceptanceFailure("browser.login.shell", "navigation_unavailable");
    }
    if (state.login_form_present) throw new BrowserAcceptanceFailure("browser.login.shell", "login_form_remained");
    throw new BrowserAcceptanceFailure("browser.login.shell", "shell_missing");
  }
}


/** 读取布局几何事实，验证根页面、侧栏和主区没有水平溢出、遮挡或按钮文本裁切。 */
async function readLayoutMetrics(page) {
  return page.evaluate(() => {
    const root = document.querySelector("[data-provider-status-root]");
    const sidebar = document.querySelector(".console-sidebar");
    const main = document.querySelector(".console-main");
    const rootRect = root?.getBoundingClientRect();
    const sidebarRect = sidebar?.getBoundingClientRect();
    const mainRect = main?.getBoundingClientRect();
    const overlapWidth = Math.max(0, Math.min(sidebarRect?.right ?? 0, mainRect?.right ?? 0)
      - Math.max(sidebarRect?.left ?? 0, mainRect?.left ?? 0));
    const overlapHeight = Math.max(0, Math.min(sidebarRect?.bottom ?? 0, mainRect?.bottom ?? 0)
      - Math.max(sidebarRect?.top ?? 0, mainRect?.top ?? 0));
    const visibleButtons = [...(root?.querySelectorAll("button") ?? [])].filter((button) => {
      const style = getComputedStyle(button);
      return style.display !== "none" && style.visibility !== "hidden" && button.getClientRects().length > 0;
    });
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      root_visible: Boolean(rootRect && rootRect.width > 0 && rootRect.height > 0),
      root_scroll_width: root?.scrollWidth ?? 0,
      root_client_width: root?.clientWidth ?? 0,
      navigation_main_overlap_area: overlapWidth * overlapHeight,
      overflowing_button_count: visibleButtons.filter(
        (button) => button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1,
      ).length,
    };
  });
}


/** 校验冻结视口布局；返回同一脱敏指标对象供写入验收证据。 */
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
  requireCondition(metrics.root_visible, stage, "root_hidden");
  requireCondition(metrics.root_scroll_width <= metrics.root_client_width + 1, stage, "root_overflow");
  requireCondition(metrics.navigation_main_overlap_area === 0, stage, "navigation_overlap");
  requireCondition(metrics.overflowing_button_count === 0, stage, "button_overflow");
  return metrics;
}


/** 捕获一个固定状态和视口，屏蔽身份、Endpoint、维护备注与故障标识后执行 PNG 像素校验。 */
async function captureState(page, config, stateName, viewport) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  await page.locator("[data-provider-status-root]").scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  const layout = assertLayoutMetrics(await readLayoutMetrics(page), viewport);
  const filename = `${stateName}-${viewport.name}.png`;
  const target = path.join(config.artifactDirectory, "screenshots", filename);
  const buffer = await page.screenshot({
    path: target,
    type: "png",
    fullPage: false,
    animations: "disabled",
    mask: [
      page.locator(".user-menu"),
      page.locator(".provider-status-identity"),
      page.locator(".provider-maintenance-note"),
      page.locator(".provider-status-failure .break-anywhere"),
    ],
    maskColor: "#d1d5db",
  });
  const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${stateName}`);
  return {
    state: stateName,
    viewport: viewport.name,
    filename,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    byte_length: buffer.length,
    ...pixels,
    layout,
  };
}


/** 等待一次 refresh 真正替换旧 PAGE-088 根节点，再返回新节点，避免响应头先到造成旧 DOM 假通过。 */
async function waitForStatusRootReplacement(page, previousRoot, stage) {
  requireCondition(Boolean(previousRoot), stage, "old_root_missing");
  await page.waitForFunction((root) => !root.isConnected, previousRoot, { timeout: 30000 });
  await previousRoot.dispose();
  const currentRoot = page.locator("[data-provider-status-root]").first();
  await currentRoot.waitFor({ state: "visible", timeout: 30000 });
  return currentRoot;
}


/** 打开 PAGE-088，冻结真实 loading，随后放行真实 200 并验证唯一 status.viewed。 */
async function openProviderStatus(page, config, scenario, tracker, screenshots) {
  const statusPromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/status/providers"
        && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  const eventPromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/console/events"
        && response.request().method() === "POST"
        && response.request().postDataJSON()?.event_type === "status.viewed";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  const heldPromise = scenario.armLoadingHold();
  await page.locator('[data-console-nav="provider-health"]').click();
  await waitForSignal(heldPromise, "browser.loading");
  const loading = page.locator("[data-provider-status-root] .provider-status-loading:not(.error-state)");
  await loading.waitFor({ state: "visible", timeout: 30000 });
  requireCondition(
    String(await loading.textContent()).includes("正在读取 Provider 状态"),
    "browser.loading",
    "text",
  );
  screenshots.push(await captureState(page, config, "loading", VIEWPORTS[0]));
  await scenario.releaseLoadingHold();
  const response = await statusPromise;
  requireCondition(response.status() === 200, "browser.status", "status");
  let payload;
  try { payload = await response.json(); } catch { payload = null; }
  const validated = validateProviderStatusPayload(payload);
  await page.locator(".provider-status-item").first().waitFor({ state: "visible", timeout: 30000 });
  const eventResponse = await eventPromise;
  requireCondition(eventResponse.status() === 200, "browser.status_viewed", "status");
  requireCondition(
    tracker.statusViewedCount === 1 && tracker.statusViewedExactCount === 1,
    "browser.status_viewed",
    "contract",
  );
  return validated;
}


/** 验证页面包含汇总、三类 P95、调用/探测计数，并展开至少一条非空最近故障。 */
async function verifyProviderSurface(page) {
  const rootText = String(await page.locator("[data-provider-status-root]").textContent());
  for (const label of [
    "Provider 归因成功率",
    "非流式调用 P95",
    "流式首块 P95",
    "探测成功率 / P95",
  ]) {
    requireCondition(rootText.includes(label), "browser.surface", "metric_label");
  }
  requireCondition(await page.locator(".provider-status-summary").count() === 1, "browser.surface", "summary");
  requireCondition(await page.locator(".provider-status-counts").count() > 0, "browser.surface", "call_counts");
  requireCondition(await page.locator(".provider-status-probe-counts").count() > 0, "browser.surface", "probe_counts");
  const details = page.locator("details.provider-status-failures:has(.provider-status-failure)").first();
  requireCondition(await details.count() === 1, "browser.surface", "failure_missing");
  await details.locator("summary").click();
  requireCondition(await details.getAttribute("open") !== null, "browser.surface", "failure_not_open");
  return { summary_visible: true, p95_groups_visible: 3, count_groups_visible: 2, failure_expanded: true };
}


/** 提交 Provider 类型筛选并重置，按动作前后请求增量验证查询而不把筛选值写入证据。 */
async function exerciseProviderTypeFilter(page, config, tracker, filterType) {
  const filteredBaseline = tracker.statusFilterRequestCount;
  const initialRoot = await page.locator("[data-provider-status-root]").first().elementHandle();
  const filteredResponse = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/status/providers"
        && hasExactSearchParams(target, { provider_type: filterType, limit: 50, offset: 0 })
        && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await page.locator("#provider-status-type-filter").selectOption(filterType);
  await page.locator('#provider-health-filters button[type="submit"]').click();
  const filtered = await filteredResponse;
  requireCondition(filtered.status() === 200, "browser.filter", "status");
  requireCondition(
    hasExactSearchParams(new URL(filtered.url()), { provider_type: filterType, limit: 50, offset: 0 }),
    "browser.filter",
    "query_contract",
  );
  await waitForStatusRootReplacement(page, initialRoot, "browser.filter");
  await page.locator("#provider-status-type-filter").waitFor({ state: "visible", timeout: 30000 });
  requireCondition(
    await page.locator("#provider-status-type-filter").inputValue() === filterType,
    "browser.filter",
    "selection",
  );
  requireCondition(tracker.statusFilterRequestCount > filteredBaseline, "browser.filter", "request_missing");

  const resetBaseline = tracker.statusResetRequestCount;
  const filteredRoot = await page.locator("[data-provider-status-root]").first().elementHandle();
  const resetResponse = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/status/providers"
        && hasExactSearchParams(target, { limit: 50, offset: 0 })
        && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await page.locator("[data-provider-health-reset]").click();
  const reset = await resetResponse;
  requireCondition(reset.status() === 200, "browser.filter_reset", "status");
  requireCondition(
    hasExactSearchParams(new URL(reset.url()), { limit: 50, offset: 0 }),
    "browser.filter_reset",
    "query_contract",
  );
  await waitForStatusRootReplacement(page, filteredRoot, "browser.filter_reset");
  await page.locator("#provider-status-type-filter").waitFor({ state: "visible", timeout: 30000 });
  requireCondition(await page.locator("#provider-status-type-filter").inputValue() === "", "browser.filter_reset", "selection");
  requireCondition(tracker.statusResetRequestCount > resetBaseline, "browser.filter_reset", "request_missing");
  return { provider_type_applied: true, reset_applied: true };
}


/** 点击真实立即探测，验证后端 POST 成功返回并触发新的状态 GET；不保留 Provider 标识。 */
async function exerciseProviderProbe(page, config, tracker) {
  const button = page.locator("[data-provider-probe]").first();
  await button.waitFor({ state: "visible", timeout: 30000 });
  const probeBaseline = tracker.probePostCount;
  const previousRoot = await page.locator("[data-provider-status-root]").first().elementHandle();
  const probeResponse = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && /^\/providers\/[^/]+\/probe$/.test(target.pathname)
        && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 60000 });
  await button.click();
  const probe = await probeResponse;
  requireCondition(probe.status() === 200, "browser.probe", "status");
  requireCondition(
    isExactProbeRequest(probe.request(), config.controlOrigin),
    "browser.probe",
    "request_contract",
  );
  // Playwright 在响应头事件发生时返回，此刻页面仍需解析探测 JSON，足以先注册后续状态刷新监听。
  const statusBaseline = tracker.statusGetCount;
  const refreshedResponse = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/status/providers"
        && hasExactSearchParams(target, { limit: 50, offset: 0 })
        && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 60000 });
  requireCondition((await refreshedResponse).status() === 200, "browser.probe", "refresh_status");
  await waitForStatusRootReplacement(page, previousRoot, "browser.probe");
  await page.locator(".provider-status-item").first().waitFor({ state: "visible", timeout: 30000 });
  requireCondition(tracker.probePostCount > probeBaseline, "browser.probe", "post_missing");
  requireCondition(tracker.statusGetCount > statusBaseline, "browser.probe", "refresh_missing");
  return { post_succeeded: true, status_refreshed: true };
}


/** 在模拟响应仍显示时检查 canary 未进入 URL、DOM 或任一 Storage，避免恢复重绘掩盖瞬时泄漏。 */
async function assertTransientCanaryAbsent(page, canary) {
  if (!canary) return true;
  const hitCount = await page.evaluate((value) => {
    const surfaces = [
      location.href,
      document.documentElement.outerHTML,
      JSON.stringify(Object.entries(localStorage)),
      JSON.stringify(Object.entries(sessionStorage)),
    ];
    return surfaces.filter((surface) => surface.includes(value)).length;
  }, canary);
  requireCondition(hitCount === 0, "browser.empty_state", "canary_exposed");
  return true;
}


/** 在同一次页面访问内验证统一 503、空 200 和恢复真实成功态，状态切换不得重复上报访问事件。 */
async function exerciseErrorEmptyAndRecovery(page, config, scenario) {
  scenario.setMode("error");
  const errorResponse = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/status/providers"
        && response.status() === 503;
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await page.locator('[data-console-nav="provider-health"]').click();
  await errorResponse;
  const error = page.locator("[data-provider-status-root] .provider-status-loading.error-state[role=\"alert\"]");
  await error.waitFor({ state: "visible", timeout: 30000 });
  const errorText = String(await error.textContent());
  requireCondition(
    errorText.includes("Provider 状态暂不可用") && errorText.includes("req_prd061_browser_503"),
    "browser.error_state",
    "unified_error",
  );

  scenario.setMode("empty");
  const emptyResponse = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/status/providers"
        && response.status() === 200;
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await page.locator('[data-console-nav="provider-health"]').click();
  await emptyResponse;
  const empty = page.locator(".provider-status-empty");
  await empty.waitFor({ state: "visible", timeout: 30000 });
  requireCondition(
    String(await empty.textContent()).includes("当前筛选条件下没有 Provider"),
    "browser.empty_state",
    "text",
  );
  const canaryAbsent = await assertTransientCanaryAbsent(page, config.sensitiveCanary);

  scenario.setMode("real");
  const recoveryResponse = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/status/providers"
        && response.status() === 200;
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await page.locator('[data-console-nav="provider-health"]').click();
  await recoveryResponse;
  await page.locator(".provider-status-item").first().waitFor({ state: "visible", timeout: 30000 });
  return {
    unified_503_visible: true,
    empty_200_visible: true,
    sensitive_canary_transiently_absent: canaryAbsent,
    real_state_recovered: true,
  };
}


/** 扫描 URL、DOM 与 Storage；Token 只允许在固定 Session 槽位，其他敏感值必须零命中。 */
async function assertBrowserPrivacy(page, config) {
  const metrics = await page.evaluate(({ password, canary, authKeys }) => {
    const html = document.documentElement.outerHTML;
    const url = location.href;
    const localEntries = Object.entries(localStorage);
    const sessionEntries = Object.entries(sessionStorage);
    const token = sessionStorage.getItem("metahub.auth_token") || "";
    const nonTokenSessionEntries = sessionEntries.filter(([key]) => key !== "metahub.auth_token");
    const allStorage = JSON.stringify([...localEntries, ...sessionEntries]);
    const sensitiveInputs = [password, canary].filter(Boolean);
    return {
      auth_slot_count: sessionEntries.filter(([key]) => authKeys.includes(key)).length,
      unknown_session_slot_count: sessionEntries.filter(([key]) => !authKeys.includes(key)).length,
      forbidden_storage_key_count: [...localEntries, ...sessionEntries].filter(
        ([key]) => !authKeys.includes(key) && /password|token|secret|prompt|response|provider[_-]?key/i.test(key),
      ).length,
      supplied_secret_hit_count: sensitiveInputs.filter(
        (secret) => [url, html, allStorage].some((value) => value.includes(secret)),
      ).length,
      // 认证 Token 只能出现在专用 token 槽，复制到 auth_context 或任何其他 Storage 也属于泄漏。
      token_outside_auth_slot: Boolean(token) && [
        url,
        html,
        JSON.stringify(localEntries),
        JSON.stringify(nonTokenSessionEntries),
      ].some(
        (value) => value.includes(token),
      ),
      bearer_hit_count: [url, html, allStorage].reduce(
        (count, value) => count + (value.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
        0,
      ),
    };
  }, { password: config.adminPassword, canary: config.sensitiveCanary, authKeys: AUTH_STORAGE_KEYS });
  requireCondition(metrics.auth_slot_count === AUTH_STORAGE_KEYS.length, "browser.privacy", "auth_slots");
  requireCondition(metrics.unknown_session_slot_count === 0, "browser.privacy", "unknown_session_slot");
  requireCondition(metrics.forbidden_storage_key_count === 0, "browser.privacy", "storage_key");
  requireCondition(metrics.supplied_secret_hit_count === 0, "browser.privacy", "secret_exposed");
  requireCondition(!metrics.token_outside_auth_slot, "browser.privacy", "token_exposed");
  requireCondition(metrics.bearer_hit_count === 0, "browser.privacy", "bearer_exposed");
  // 成功证据只表达零命中结论，不返回被扫描的 URL、DOM、Storage 或认证值。
  return { auth_storage_contract_valid: true, sensitive_url_dom_storage_hits: 0 };
}


/** 执行真实注销，等待登录页后再确认认证槽位与 PAGE-088 业务 DOM 已全部销毁。 */
async function logoutAndVerify(page, config) {
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/auth/logout"
        && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await page.locator("#logout-button").click();
  requireCondition((await responsePromise).status() === 200, "browser.logout", "status");
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30000 });
  const cleared = await page.evaluate((authKeys) => ({
    auth_slot_count: authKeys.filter((key) => sessionStorage.getItem(key) !== null).length,
    protected_dom_count: document.querySelectorAll("[data-provider-status-root],.provider-status-page,.console-shell").length,
  }), AUTH_STORAGE_KEYS);
  requireCondition(cleared.auth_slot_count === 0, "browser.logout", "auth_slots");
  requireCondition(cleared.protected_dom_count === 0, "browser.logout", "protected_dom");
  // 注销证据只保留清理结论，Session 和已销毁页面正文都不能返回调用方。
  return { auth_storage_cleared: true, protected_dom_cleared: true };
}


/** 深度校验成功证据不含敏感字段名或调用方提供的敏感原值。 */
function assertSafeEvidence(evidence, secrets = []) {
  const visit = (value) => {
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!value || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      requireCondition(!FORBIDDEN_EVIDENCE_KEY.test(key), "evidence.privacy", "forbidden_key");
      visit(child);
    }
  };
  visit(evidence);
  const serialized = JSON.stringify(evidence);
  for (const secret of secrets.filter(Boolean)) {
    requireCondition(!serialized.includes(secret), "evidence.privacy", "secret_value");
  }
  return evidence;
}


/** 独占发布脱敏 JSON；新 run-id 中仍使用 wx，防止并发进程覆盖同一批次事实。 */
async function publishEvidence(directory, evidence) {
  try {
    await writeFile(
      path.join(directory, "browser-acceptance.json"),
      `${JSON.stringify(evidence, null, 2)}\n`,
      { encoding: "utf8", flag: "wx" },
    );
  } catch {
    throw new BrowserAcceptanceFailure("evidence.publish", "write_failed");
  }
}


/** 启动离线 Chromium，完成 loading、成功、筛选、故障、探测、503、空态、恢复、隐私和注销验收。 */
async function executeAcceptance(config) {
  let chromium;
  try {
    ({ chromium } = require("playwright"));
  } catch {
    throw new BrowserAcceptanceFailure("browser.playwright", "module_missing");
  }
  let browser = null;
  let context = null;
  try {
    browser = await runBrowserOperation("browser.launch", () => chromium.launch({
      executablePath: config.chromePath,
      headless: true,
      downloadsPath: path.join(config.artifactDirectory, "downloads"),
      env: buildBrowserEnvironment(process.env, config.artifactDirectory),
      args: [
        "--disable-background-networking",
        "--disable-breakpad",
        "--disable-component-update",
        "--disable-crash-reporter",
        "--disable-default-apps",
        "--disable-domain-reliability",
        "--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter",
        "--disable-gpu",
        "--no-default-browser-check",
        "--no-first-run",
      ],
    }));
    context = await runBrowserOperation("browser.context", () => browser.newContext({
      viewport: { width: VIEWPORTS[0].width, height: VIEWPORTS[0].height },
      locale: "zh-CN",
      serviceWorkers: "block",
      acceptDownloads: false,
    }));
    const page = await runBrowserOperation("browser.page", () => context.newPage());
    // 可选 canary 只用于模拟响应的前端泄漏检查，不会写入 tracker 或证据。
    const scenario = createStatusScenario(config.sensitiveCanary);
    const tracker = await runBrowserOperation(
      "browser.guards",
      () => installBrowserGuards(context, page, config, scenario),
    );
    const screenshots = [];
    await runBrowserOperation("browser.login", () => loginAdministrator(page, config, tracker));
    const status = await runBrowserOperation(
      "browser.status",
      () => openProviderStatus(page, config, scenario, tracker, screenshots),
    );
    const surface = await runBrowserOperation("browser.surface", () => verifyProviderSurface(page));
    screenshots.push(await runBrowserOperation(
      "browser.screenshot.desktop",
      () => captureState(page, config, "success", VIEWPORTS[0]),
    ));
    screenshots.push(await runBrowserOperation(
      "browser.screenshot.mobile",
      () => captureState(page, config, "success", VIEWPORTS[1]),
    ));
    await page.setViewportSize({ width: VIEWPORTS[0].width, height: VIEWPORTS[0].height });
    const filter = await runBrowserOperation(
      "browser.filter",
      () => exerciseProviderTypeFilter(page, config, tracker, status.filterType),
    );
    const probe = await runBrowserOperation(
      "browser.probe",
      () => exerciseProviderProbe(page, config, tracker),
    );
    const states = await runBrowserOperation(
      "browser.states",
      () => exerciseErrorEmptyAndRecovery(page, config, scenario),
    );
    const privacy = await runBrowserOperation("browser.privacy", () => assertBrowserPrivacy(page, config));
    requireCondition(
      tracker.statusViewedCount === 1 && tracker.statusViewedExactCount === 1,
      "browser.status_viewed",
      "duplicate_or_invalid",
    );
    requireCondition(tracker.blockedRequestCount === 0, "browser.network", "external_request");
    requireCondition(tracker.pageErrorCount === 0, "browser.errors", "page_error");
    requireCondition(tracker.consoleErrorCount === 0, "browser.errors", "console_error");
    const logout = await runBrowserOperation("browser.logout", () => logoutAndVerify(page, config));
    requireCondition(screenshots.length === 3, "browser.screenshots", "count");
    requireCondition(screenshots.every((item) => item.non_blank_pixels), "browser.screenshots", "blank");

    const evidence = {
      schema_version: 1,
      prd_id: "PRD-061",
      page_id: "PAGE-088",
      status: "passed",
      checks: {
        http_statuses: {
          login_200: tracker.loginStatuses.includes(200),
          status_real_200: tracker.statusStatuses.includes(200),
          status_simulated_503: tracker.statusStatuses.includes(503),
          probe_200: tracker.probeStatuses.includes(200),
          event_200: tracker.eventStatuses.includes(200),
          logout_200: tracker.logoutStatuses.includes(200),
        },
        loading_visible: true,
        provider_present: status.provider_present,
        surface,
        // 只记录控件动作是否完成，键名和值都不携带筛选字段或实际 Provider 类型。
        query_controls: filter,
        probe,
        states,
        status_viewed: { exactly_once: true, exact_contract: true },
        privacy,
        logout,
        network: { external_request_count: 0 },
        browser_errors: { page_error_count: 0, console_error_count: 0 },
        screenshots: { count: 3, all_non_blank: true },
      },
      screenshots,
    };
    assertSafeEvidence(evidence, [config.adminPassword, config.sensitiveCanary]);
    await publishEvidence(config.artifactDirectory, evidence);
    return evidence;
  } finally {
    await context?.close().catch(() => undefined);
    await browser?.close().catch(() => undefined);
  }
}


/** CLI 入口只输出固定成功摘要或脱敏 stage/code；证据路径和敏感配置均不进入终端。 */
async function main() {
  try {
    const config = await readConfiguration();
    const evidence = await executeAcceptance(config);
    process.stdout.write(`${JSON.stringify({
      status: evidence.status,
      prd_id: evidence.prd_id,
      page_id: evidence.page_id,
      screenshot_count: evidence.screenshots.length,
    })}\n`);
    return 0;
  } catch (error) {
    const failure = error instanceof BrowserAcceptanceFailure
      ? { stage: error.stage, code: error.code }
      : { stage: "browser.acceptance", code: "unexpected_failure" };
    process.stderr.write(`${JSON.stringify({ status: "failed", ...failure })}\n`);
    return 1;
  }
}


export {
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertLayoutMetrics,
  assertSafeEvidence,
  buildBrowserEnvironment,
  browserRequestAllowed,
  classifyBrowserConsoleMessage,
  hasExactSearchParams,
  isExactProbeRequest,
  isExactStatusViewedEvent,
  normalizeLoopbackOrigin,
  resolveArtifactDirectory,
  validateProviderStatusPayload,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
