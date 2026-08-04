/**
 * PRD-063 PAGE-090 国内模型 Endpoint 适配规范真实浏览器验收器。
 *
 * 安全边界：脚本只访问固定 localhost 服务。浏览器 HOME、TEMP、TMP、下载、截图与脱敏证据
 * 只能写入 `D:/MetaHub/.artifacts/prd063-browser/<run-id>` 的全新普通目录；密码、Session Token、
 * tenant_id、Provider Endpoint 和目录响应正文不会进入终端或证据 JSON。
 */

import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { lstat, mkdir, realpath, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";

/** CommonJS 桥只在真实执行时加载桌面运行时提供的 Playwright，不增加 Console 生产依赖。 */
const require = createRequire(import.meta.url);
/** 所有固定目录从脚本位置反算，当前工作目录不能改变证据归属。 */
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
/** PRD-063 浏览器证据唯一允许的父目录。 */
const ARTIFACT_ROOT = path.join(REPOSITORY_ROOT, ".artifacts", "prd063-browser");
/** 登录期间仅允许 Console 已有的两个 Session Storage 槽位。 */
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);
/** Excel 冻结的桌面和移动视口。 */
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
/** 激活目录身份必须与 Shared SDK 冻结合同一致。 */
const CATALOG_VERSION = "adapter-spec-v1";
const CATALOG_HASH = "9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870";
/** 三个页面分类与实际响应应包含的 Provider 类型。客户视图请求无分类参数，因此返回完整九类目录。 */
const RESPONSE_TYPES = Object.freeze({
  default: Object.freeze(["dashscope", "deepseek", "hunyuan", "volcengine", "wenxin", "zhipu"]),
  customer: Object.freeze([
    "custom",
    "dashscope",
    "deepseek",
    "hunyuan",
    "migration_compatible",
    "openai_compatible",
    "volcengine",
    "wenxin",
    "zhipu",
  ]),
  migration: Object.freeze(["migration_compatible"]),
});
/** 三个页面分类真正可见的行，用于验证客户端并集筛选没有混入竞品或默认厂家。 */
const VISIBLE_TYPES = Object.freeze({
  default: RESPONSE_TYPES.default,
  customer: Object.freeze(["custom", "openai_compatible"]),
  migration: RESPONSE_TYPES.migration,
});
/** Provider 类型到产品分类的冻结映射。 */
const PROVIDER_CLASSIFICATION = Object.freeze({
  zhipu: "model_vendor_cloud",
  dashscope: "model_vendor_cloud",
  wenxin: "model_vendor_cloud",
  volcengine: "model_vendor_cloud",
  hunyuan: "model_vendor_cloud",
  deepseek: "model_vendor_cloud",
  openai_compatible: "local_compatible",
  custom: "customer_managed",
  migration_compatible: "migration_platform",
});
/** 单条目录合同必须完整提供的有界文本字段。 */
const SPEC_TEXT_FIELDS = Object.freeze([
  "spec_id",
  "provider_type",
  "display_name",
  "classification",
  "adapter_name",
  "adapter_version",
  "protocol_family",
  "model_id_strategy",
  "usage_profile",
  "error_profile",
  "stream_profile",
  "probe_profile",
  "status",
  "contract_case_id",
]);
/** 价格字段顺序属于运行合同，不能由页面自行猜测或重排。 */
const PRICE_FIELDS = Object.freeze([
  "input_price_per_1k",
  "output_price_per_1k",
  "currency",
  "price_schema_version",
]);
/** 证据键名禁止保留易误用的业务秘密槽位，即使值已经脱敏也不允许。 */
const FORBIDDEN_EVIDENCE_KEY = /password|token|secret|prompt|response|authorization|cookie|provider[_-]?key|maintenance[_-]?note|base[_-]?url|endpoint|filter|tenant/i;
/** URL 查询中禁止出现的敏感字段，避免浏览器历史和代理日志留痕。 */
const FORBIDDEN_QUERY_KEY = /password|token|secret|prompt|response|authorization|cookie|provider[_-]?key|tenant[_-]?id|base[_-]?url|endpoint/i;
/** 后端公开目录 DTO 不得夹带租户、凭据或可调用 Endpoint 字段。 */
const FORBIDDEN_PAYLOAD_KEY = /password|token|secret|prompt|response|authorization|cookie|provider[_-]?key|base[_-]?url|endpoint/i;


/** 使用固定 stage/code 表达失败，避免 Playwright 原错、URL 或 DOM 正文进入日志。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存稳定失败分类，不复制外部输入。 */
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 失败关闭断言；stage/code 只接受源码内固定常量。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** 把浏览器 API 原始异常折叠为固定分类，禁止打印可能含口令或页面正文的错误。 */
async function runBrowserOperation(stage, operation) {
  try {
    return await operation();
  } catch (error) {
    // 已归类的业务失败必须保留原 stage/code，避免二次包装后丢失可验收的失败原因。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "operation_failed");
  }
}


/** 只接受固定端口的 localhost HTTP Origin，不允许路径、查询、Hash、凭据或别名。 */
function normalizeLoopbackOrigin(value, expectedPort, stage = "config.origin") {
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


/**
 * 解析调用方证据目录。
 * 只允许固定证据根下一级 run-id，拒绝根目录本身、嵌套目录、绝对逃逸和路径穿越。
 */
function resolveArtifactDirectory(value, repositoryRoot = REPOSITORY_ROOT) {
  const root = path.resolve(repositoryRoot, ".artifacts", "prd063-browser");
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


/** 校验目录既不是链接节点，也没有经 reparse point 解析到其他物理位置。 */
async function assertPlainDirectory(directory, stage) {
  let information;
  let canonical;
  try {
    information = await lstat(directory);
    canonical = await realpath(directory);
  } catch {
    throw new BrowserAcceptanceFailure(stage, "missing_or_unreadable");
  }
  requireCondition(information.isDirectory() && !information.isSymbolicLink(), stage, "not_plain_directory");
  requireCondition(
    path.normalize(canonical).toLowerCase() === path.normalize(directory).toLowerCase(),
    stage,
    "reparse_point",
  );
  return directory;
}


/** 独占创建全新批次和固定子目录；任何既有目录或链接都失败，绝不覆盖历史证据。 */
async function createArtifactDirectory(value) {
  const target = resolveArtifactDirectory(value);
  const artifactsRoot = path.dirname(ARTIFACT_ROOT);
  await assertPlainDirectory(artifactsRoot, "config.artifacts_root");
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
    // 保留目录安全校验的稳定错误码；其余文件系统细节统一折叠，防止宿主路径进入证据。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure("config.artifact_directory", "create_failed");
  }
  return target;
}


/** 读取固定服务、管理员身份、Chrome 和证据目录；敏感值只保留在当前进程内存。 */
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
  const tenantSlug = String(environment.METAHUB_ACCEPTANCE_TENANT || "default");
  const adminEmail = String(environment.METAHUB_ACCEPTANCE_EMAIL || "admin@metahub.local");
  const sensitiveCanary = String(environment.METAHUB_ACCEPTANCE_SENSITIVE_CANARY || "");
  requireCondition(path.isAbsolute(chromePath) && existsSync(chromePath), "config.chrome", "missing");
  requireCondition(adminPassword.length >= 12, "config.identity", "password_missing");
  requireCondition(tenantSlug.length > 0 && tenantSlug.length <= 80, "config.identity", "tenant_invalid");
  requireCondition(adminEmail.length > 3 && adminEmail.length <= 254, "config.identity", "email_invalid");
  requireCondition(sensitiveCanary.length <= 256, "config.identity", "canary_invalid");
  // 只有所有只读配置通过后才创建目录，配置失败不得留下看似有效的空验收批次。
  const artifactDirectory = await createArtifactDirectory(
    environment.METAHUB_ACCEPTANCE_ARTIFACT_DIR,
  );
  return Object.freeze({
    artifactDirectory,
    consoleOrigin,
    controlOrigin,
    gatewayOrigin,
    chromePath,
    tenantSlug,
    adminEmail,
    adminPassword,
    sensitiveCanary,
  });
}


/** 浏览器仅继承 Windows 启动 allowlist，并把全部用户和临时目录绑定到当前批次。 */
function buildBrowserEnvironment(environment, artifactDirectory) {
  const sanitized = {};
  const requiredName = /^(?:path|pathext|systemroot|windir|comspec|os|number_of_processors|processor_(?:architecture|identifier)|lang|lc_[a-z_]+)$/i;
  for (const [key, value] of Object.entries(environment)) {
    // 未知环境变量可能是 PAT、Access Key 或客户凭据，默认不传给浏览器子进程。
    if (requiredName.test(key)) sanitized[key] = value;
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


/**
 * 在加载 Playwright 前钉住父进程可写根。
 * Playwright 的临时 profile 由 Node 父进程创建，仅设置 Chromium 子进程 env 仍可能回退系统 TEMP。
 */
function pinBrowserWritableRoots(environment, artifactDirectory) {
  const browserHome = path.join(artifactDirectory, "browser-home");
  Object.assign(environment, {
    TEMP: artifactDirectory,
    TMP: artifactDirectory,
    HOME: browserHome,
    USERPROFILE: browserHome,
    LOCALAPPDATA: path.join(browserHome, "local"),
    APPDATA: path.join(browserHome, "roaming"),
  });
  return environment;
}


/** 只放行三个本机 Origin 以及浏览器内部 URL，并拒绝 URL 凭据和敏感查询键。 */
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


/** 判断 URL 是否精确包含唯一查询参数集合，拒绝重复键、缺项和任何额外字段。 */
function hasExactSearchParams(target, expected) {
  const expectedEntries = Object.entries(expected).map(([key, value]) => [key, String(value)]);
  const actualEntries = [...target.searchParams.entries()];
  if (actualEntries.length !== expectedEntries.length) return false;
  return expectedEntries.every(([key, value]) => (
    target.searchParams.getAll(key).length === 1 && target.searchParams.get(key) === value
  ));
}


/**
 * 识别 PAGE-090 三种精确 GET 合同。
 * 请求必须携带 Bearer Session 和 Console request_id；返回 null 表示网络前合同失败。
 */
function adapterSpecRequestView(request, controlOrigin) {
  let target;
  try {
    target = new URL(request.url());
  } catch {
    return null;
  }
  const headers = request.headers?.() ?? {};
  const authorization = String(headers.authorization ?? headers.Authorization ?? "");
  const requestId = String(headers["x-request-id"] ?? headers["X-Request-ID"] ?? "");
  if (
    target.origin !== controlOrigin
    || target.pathname !== "/adapter-specs"
    || request.method() !== "GET"
    || !/^Bearer\s+\S{8,}$/.test(authorization)
    || !/^console_adapter_specs_\d+$/.test(requestId)
  ) return null;
  if (hasExactSearchParams(target, { classification: "model_vendor_cloud" })) return "default";
  if ([...target.searchParams].length === 0) return "customer";
  if (hasExactSearchParams(target, { classification: "migration_platform" })) return "migration";
  return null;
}


/** 深度检查公开 DTO 键名，任何租户、凭据或可调用地址槽位都会让整批响应失败。 */
function hasForbiddenPayloadKey(value) {
  const pending = [value];
  const visited = new Set();
  while (pending.length > 0) {
    const current = pending.pop();
    if (!current || typeof current !== "object" || visited.has(current)) continue;
    visited.add(current);
    for (const [key, child] of Object.entries(current)) {
      if (FORBIDDEN_PAYLOAD_KEY.test(key)) return true;
      if (child && typeof child === "object") pending.push(child);
    }
  }
  return false;
}


/** 验证数组由唯一、有界、无控制字符的字符串组成。 */
function validStringArray(value, maximumItems, maximumLength, { allowEmpty = false } = {}) {
  if (!Array.isArray(value) || value.length > maximumItems || (!allowEmpty && value.length === 0)) {
    return false;
  }
  const valid = value.every((item) => (
    typeof item === "string"
    && item.length > 0
    && item.length <= maximumLength
    && !/[\u0000-\u001f\u007f]/.test(item)
  ));
  return valid && new Set(value).size === value.length;
}


/**
 * 验证真实 `/adapter-specs` 响应的冻结目录身份、分类、字段和隐私边界。
 * 返回值只含视图与数量，不复制任何响应正文到 tracker 或证据。
 */
function validateAdapterCatalogPayload(payload, expectedView) {
  const expectedTypes = RESPONSE_TYPES[expectedView];
  requireCondition(Boolean(expectedTypes), "browser.catalog_payload", "view");
  requireCondition(
    payload && typeof payload === "object" && !Array.isArray(payload),
    "browser.catalog_payload",
    "object",
  );
  requireCondition(!hasForbiddenPayloadKey(payload), "browser.catalog_payload", "sensitive_key");
  requireCondition(
    payload.catalog && typeof payload.catalog === "object" && !Array.isArray(payload.catalog),
    "browser.catalog_payload",
    "catalog",
  );
  requireCondition(payload.catalog.version === CATALOG_VERSION, "browser.catalog_payload", "version");
  requireCondition(payload.catalog.hash === CATALOG_HASH, "browser.catalog_payload", "hash");
  requireCondition(
    typeof payload.catalog.activated_at === "string"
      && payload.catalog.activated_at.length <= 64
      && !Number.isNaN(Date.parse(payload.catalog.activated_at)),
    "browser.catalog_payload",
    "activated_at",
  );
  requireCondition(
    typeof payload.request_id === "string" && payload.request_id.length > 0 && payload.request_id.length <= 160,
    "browser.catalog_payload",
    "request_id",
  );
  requireCondition(Array.isArray(payload.specs), "browser.catalog_payload", "specs");
  const actualTypes = [];
  const specIds = [];
  const tenantIds = [];
  for (const spec of payload.specs) {
    requireCondition(spec && typeof spec === "object" && !Array.isArray(spec), "browser.catalog_payload", "item");
    // tenant_id 是后端证明当前 Session 租户归属的必要字段；只做一致性判断，绝不复制到返回摘要或证据。
    requireCondition(
      typeof spec.tenant_id === "string"
        && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(spec.tenant_id),
      "browser.catalog_payload",
      "tenant_id",
    );
    for (const field of SPEC_TEXT_FIELDS) {
      requireCondition(
        typeof spec[field] === "string"
          && spec[field].trim().length > 0
          && spec[field].length <= 160
          && !/[\u0000-\u001f\u007f]/.test(spec[field]),
        "browser.catalog_payload",
        "text_field",
      );
    }
    requireCondition(
      PROVIDER_CLASSIFICATION[spec.provider_type] === spec.classification,
      "browser.catalog_payload",
      "classification",
    );
    requireCondition(
      ["openai_compatible", "generic_http"].includes(spec.adapter_name),
      "browser.catalog_payload",
      "adapter",
    );
    requireCondition(["active", "optional"].includes(spec.status), "browser.catalog_payload", "status");
    requireCondition(
      validStringArray(spec.allowed_auth_types, 4, 40)
        && spec.allowed_auth_types.every((item) => ["none", "bearer"].includes(item)),
      "browser.catalog_payload",
      "auth_types",
    );
    requireCondition(
      validStringArray(spec.price_fields, 12, 80)
        && JSON.stringify(spec.price_fields) === JSON.stringify(PRICE_FIELDS),
      "browser.catalog_payload",
      "price_fields",
    );
    requireCondition(
      validStringArray(spec.platform_examples, 8, 80, { allowEmpty: true }),
      "browser.catalog_payload",
      "platform_examples",
    );
    const expectedExamples = spec.provider_type === "migration_compatible" ? ["OpenRouter", "硅基流动"] : [];
    requireCondition(
      JSON.stringify(spec.platform_examples) === JSON.stringify(expectedExamples),
      "browser.catalog_payload",
      "platform_boundary",
    );
    actualTypes.push(spec.provider_type);
    specIds.push(spec.spec_id);
    tenantIds.push(spec.tenant_id);
  }
  requireCondition(new Set(tenantIds).size === 1, "browser.catalog_payload", "tenant_mismatch");
  requireCondition(new Set(specIds).size === specIds.length, "browser.catalog_payload", "duplicate_spec_id");
  requireCondition(new Set(actualTypes).size === actualTypes.length, "browser.catalog_payload", "duplicate_type");
  requireCondition(
    JSON.stringify([...actualTypes].sort()) === JSON.stringify(expectedTypes),
    "browser.catalog_payload",
    "type_set",
  );
  return Object.freeze({ view: expectedView, entry_count: actualTypes.length });
}


/** 浏览器 console 只要出现 error 就失败；本验收不主动制造任何可豁免的 HTTP 错误。 */
function classifyBrowserConsoleMessage({ type } = {}) {
  return type === "error" ? "unexpected_error" : "ignored";
}


/** 安装 loopback 路由门禁和脱敏计数器；请求正文、Header 和响应正文都不写入 tracker。 */
async function installBrowserGuards(context, page, config) {
  const allowedOrigins = new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]);
  const tracker = {
    blockedRequestCount: 0,
    invalidCatalogRequestCount: 0,
    pageErrorCount: 0,
    consoleErrorCount: 0,
    catalogRequestCounts: { default: 0, customer: 0, migration: 0 },
    loginStatuses: [],
    meStatuses: [],
    navigationStatuses: [],
    catalogStatuses: [],
    logoutStatuses: [],
  };
  page.on("pageerror", () => { tracker.pageErrorCount += 1; });
  page.on("console", (message) => {
    // 浏览器普通信息不应误伤验收，仅未预期 error 级输出计入失败指标。
    if (classifyBrowserConsoleMessage({ type: message.type() }) === "unexpected_error") {
      tracker.consoleErrorCount += 1;
    }
  });
  page.on("response", (response) => {
    try {
      const target = new URL(response.url());
      if (target.origin !== config.controlOrigin) return;
      const method = response.request().method();
      // 分接口保存整数状态码，不保留响应正文，既验证真实登录链路又避免敏感信息落盘。
      if (target.pathname === "/auth/login" && method === "POST") {
        tracker.loginStatuses.push(response.status());
      }
      // `/auth/me` 单独证明登录后的服务端身份有效，不能用登录接口成功替代权限会话校验。
      if (target.pathname === "/auth/me" && method === "GET") {
        tracker.meStatuses.push(response.status());
      }
      if (target.pathname === "/console/navigation" && method === "GET") {
        tracker.navigationStatuses.push(response.status());
      }
      if (target.pathname === "/adapter-specs" && method === "GET") {
        tracker.catalogStatuses.push(response.status());
      }
      // 注销状态独立记录，用于证明服务端会话失效，而不是只依赖前端清理后的页面外观。
      if (target.pathname === "/auth/logout" && method === "POST") {
        tracker.logoutStatuses.push(response.status());
      }
    } catch {
      // 非法 URL 会被总路由门禁拒绝；响应监听器只保存本机整数状态码。
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
    if (target.origin === config.controlOrigin && target.pathname === "/adapter-specs") {
      const view = adapterSpecRequestView(request, config.controlOrigin);
      if (!view) {
        tracker.invalidCatalogRequestCount += 1;
        await route.abort("blockedbyclient");
        return;
      }
      tracker.catalogRequestCounts[view] += 1;
    }
    await route.continue();
  });
  return tracker;
}


/** 通过真实登录表单建立管理员 Session；密码只传给输入框，不读取或输出 Token。 */
async function loginAdministrator(page, config, tracker) {
  await page.goto(`${config.consoleOrigin}/`, { waitUntil: "domcontentloaded" });
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30000 });
  await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
  await form.locator('[name="email"]').fill(config.adminEmail);
  await form.locator('[name="password"]').fill(config.adminPassword);
  const [response] = await Promise.all([
    page.waitForResponse((candidate) => {
      try {
        const target = new URL(candidate.url());
        return target.origin === config.controlOrigin
          && target.pathname === "/auth/login"
          && candidate.request().method() === "POST";
      } catch {
        return false;
      }
    }, { timeout: 30000 }),
    form.locator('button[type="submit"]').click(),
  ]);
  requireCondition(response.status() === 200, "browser.login", "status");
  await Promise.all([
    page.locator("#logout-button").waitFor({ state: "visible", timeout: 30000 }),
    page.locator(".console-shell").waitFor({ state: "visible", timeout: 30000 }),
  ]);
  const administratorConfirmed = await page.evaluate(() => {
    try {
      const context = JSON.parse(sessionStorage.getItem("metahub.auth_context") || "null");
      return context?.user?.role === "admin";
    } catch {
      return false;
    }
  });
  requireCondition(administratorConfirmed, "browser.login", "administrator_required");
  requireCondition(tracker.meStatuses.includes(200), "browser.login", "identity_status");
  requireCondition(tracker.navigationStatuses.includes(200), "browser.login", "navigation_status");
  return { administrator_confirmed: true };
}


/** 等待页面根节点被完整刷新替换，防止响应头先到时读取旧分类 DOM。 */
async function waitForCatalogRootReplacement(page, previousRoot, stage) {
  requireCondition(Boolean(previousRoot), stage, "old_root_missing");
  await page.waitForFunction((root) => !root.isConnected, previousRoot, { timeout: 30000 });
  await previousRoot.dispose();
  const current = page.locator("[data-adapter-spec-root]").first();
  await current.waitFor({ state: "visible", timeout: 30000 });
  return current;
}


/** 等待并读取指定分类的真实 200 响应，正文仅在内存中完成合同校验。 */
async function readCatalogResponse(response, expectedView) {
  requireCondition(response.status() === 200, "browser.catalog", "status");
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const summary = validateAdapterCatalogPayload(payload, expectedView);
  // tenant_id 只短暂用于证明前端白名单已从 DOM 删除，返回上层证据前必须显式拆除。
  return { summary, tenantId: payload.specs[0].tenant_id };
}


/** 构造指定分类的响应监听器；精确请求合同由同一个纯函数判断。 */
function waitForCatalogResponse(page, config, expectedView) {
  return page.waitForResponse(
    (response) => adapterSpecRequestView(response.request(), config.controlOrigin) === expectedView,
    { timeout: 30000 },
  );
}


/** 验证当前分类的可见行、分段按钮、目录身份、敏感 DOM 与迁移平台边界。 */
async function assertCatalogViewDom(page, expectedView, tenantId) {
  const expectedTypes = VISIBLE_TYPES[expectedView];
  requireCondition(Boolean(expectedTypes), "browser.catalog_dom", "view");
  const root = page.locator("[data-adapter-spec-root]").first();
  await root.waitFor({ state: "visible", timeout: 30000 });
  await root.locator(".adapter-spec-row[data-provider-type]").first().waitFor({ state: "visible", timeout: 30000 });
  const facts = await root.evaluate((element, currentTenantId) => {
    const rows = [...element.querySelectorAll(".adapter-spec-row[data-provider-type]")];
    const hashNode = element.querySelector(".adapter-spec-catalog dd[title]");
    const activeButton = element.querySelector('[data-adapter-spec-view][aria-pressed="true"]');
    const boundary = element.querySelector(".adapter-spec-boundary");
    return {
      types: rows.map((row) => row.getAttribute("data-provider-type") || "").sort(),
      heading_ok: element.textContent.includes("国内模型 Endpoint 适配规范"),
      table_labels_ok: ["Provider", "Adapter", "鉴权", "模型 ID", "Usage", "错误", "流式", "价格字段"].every(
        (label) => element.textContent.includes(label),
      ),
      version_ok: element.textContent.includes("adapter-spec-v1"),
      hash_ok: hashNode?.getAttribute("title") === "9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870",
      request_trace_ok: ![...element.querySelectorAll(".adapter-spec-catalog dd")].some(
        (node) => node.textContent.trim() === "-",
      ),
      active_view: activeButton?.getAttribute("data-adapter-spec-view") || "",
      boundary_count: element.querySelectorAll(".adapter-spec-boundary").length,
      boundary_ok: Boolean(boundary)
        && boundary.textContent.includes("OpenRouter、硅基流动只用于对标或受控迁移兼容")
        && boundary.textContent.includes("不是模型厂家，也不是默认上游"),
      migration_examples_ok: element.textContent.includes("OpenRouter / 硅基流动"),
      sensitive_control_count: element.querySelectorAll(
        '[name="tenant_id"],[name*="password" i],[name*="token" i],[name*="secret" i],[name*="base_url" i],[data-provider-endpoint]',
      ).length,
      url_text_count: (element.textContent.match(/https?:\/\//gi) ?? []).length,
      tenant_field_name_absent: !element.outerHTML.includes("tenant_id"),
      tenant_value_absent: !element.outerHTML.includes(currentTenantId),
    };
  }, tenantId);
  requireCondition(
    JSON.stringify(facts.types) === JSON.stringify(expectedTypes),
    "browser.catalog_dom",
    "type_set",
  );
  requireCondition(facts.heading_ok && facts.table_labels_ok, "browser.catalog_dom", "surface");
  requireCondition(facts.version_ok && facts.hash_ok && facts.request_trace_ok, "browser.catalog_dom", "identity");
  requireCondition(facts.active_view === expectedView, "browser.catalog_dom", "active_view");
  requireCondition(facts.sensitive_control_count === 0 && facts.url_text_count === 0, "browser.catalog_dom", "privacy");
  requireCondition(
    facts.tenant_field_name_absent && facts.tenant_value_absent,
    "browser.catalog_dom",
    "tenant_exposed",
  );
  if (expectedView === "migration") {
    requireCondition(
      facts.boundary_count === 1 && facts.boundary_ok && facts.migration_examples_ok,
      "browser.catalog_dom",
      "migration_boundary",
    );
  } else {
    requireCondition(facts.boundary_count === 0, "browser.catalog_dom", "boundary_leaked");
  }
  return Object.freeze({ view: expectedView, visible_entry_count: facts.types.length });
}


/** 读取页面、侧栏、合同表和按钮几何事实，不返回任何 DOM 文本。 */
async function readLayoutMetrics(page) {
  return page.evaluate(() => {
    const root = document.querySelector("[data-adapter-spec-root]");
    const sidebar = document.querySelector(".console-sidebar");
    const main = document.querySelector(".console-main");
    const table = root?.querySelector(".adapter-spec-table-wrap");
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
      root_horizontal_inside: Boolean(rootRect && rootRect.left >= -1 && rootRect.right <= innerWidth + 1),
      root_scroll_width: root?.scrollWidth ?? 0,
      root_client_width: root?.clientWidth ?? 0,
      table_scroll_width: table?.scrollWidth ?? 0,
      table_client_width: table?.clientWidth ?? 0,
      navigation_main_overlap_area: overlapWidth * overlapHeight,
      overflowing_button_count: visibleButtons.filter(
        (button) => button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1,
      ).length,
      empty_button_text_count: visibleButtons.filter((button) => !button.textContent.trim()).length,
      view_button_count: root?.querySelectorAll("[data-adapter-spec-view]").length ?? 0,
      visible_row_count: root?.querySelectorAll(".adapter-spec-row[data-provider-type]").length ?? 0,
    };
  });
}


/** 校验冻结视口没有页面横溢、侧栏遮挡、按钮裁切或移动合同表横溢。 */
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
  requireCondition(metrics.root_visible && metrics.root_horizontal_inside, stage, "root_hidden_or_outside");
  requireCondition(metrics.root_scroll_width <= metrics.root_client_width + 1, stage, "root_overflow");
  requireCondition(metrics.navigation_main_overlap_area === 0, stage, "navigation_overlap");
  requireCondition(metrics.overflowing_button_count === 0, stage, "button_overflow");
  requireCondition(metrics.empty_button_text_count === 0 && metrics.view_button_count === 3, stage, "button_contract");
  requireCondition(metrics.visible_row_count > 0, stage, "rows_missing");
  if (viewport.width <= 720) {
    requireCondition(metrics.table_scroll_width <= metrics.table_client_width + 1, stage, "mobile_table_overflow");
  }
  return metrics;
}


/** 捕获固定分类与视口，屏蔽身份后执行尺寸和非空像素检查。 */
async function captureViewport(page, config, view, viewport) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  // 视口切换会保留上一分类的滚动位置；重新对齐页面标题，避免移动端证据从目录中段开始而丢失页面身份。
  await page.locator(".adapter-spec-heading").scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  const layout = assertLayoutMetrics(await readLayoutMetrics(page), viewport);
  const filename = `${view}-${viewport.name}.png`;
  const target = path.join(config.artifactDirectory, "screenshots", filename);
  const buffer = await page.screenshot({
    path: target,
    type: "png",
    fullPage: false,
    animations: "disabled",
    mask: [page.locator(".user-menu")],
    maskColor: "#d1d5db",
  });
  const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${view}`);
  return {
    view,
    viewport: viewport.name,
    filename,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    byte_length: buffer.length,
    ...pixels,
    layout,
  };
}


/** 打开 PAGE-090 并依次验收默认厂家、客户 Endpoint 与迁移兼容三类真实响应和 DOM。 */
async function exerciseCatalogViews(page, config) {
  const screenshots = [];
  const allVisibleTypes = new Set();
  const [defaultResponse] = await Promise.all([
    waitForCatalogResponse(page, config, "default"),
    page.locator('[data-console-nav="adapter-specs"]').click(),
  ]);
  const defaultPayload = await readCatalogResponse(defaultResponse, "default");
  const defaultDom = await assertCatalogViewDom(page, "default", defaultPayload.tenantId);
  VISIBLE_TYPES.default.forEach((item) => allVisibleTypes.add(item));
  screenshots.push(await captureViewport(page, config, "default", VIEWPORTS[0]));

  const defaultRoot = await page.locator("[data-adapter-spec-root]").first().elementHandle();
  const [customerResponse] = await Promise.all([
    waitForCatalogResponse(page, config, "customer"),
    page.locator('[data-adapter-spec-view="customer"]').click(),
  ]);
  const customerPayload = await readCatalogResponse(customerResponse, "customer");
  await waitForCatalogRootReplacement(page, defaultRoot, "browser.customer");
  const customerDom = await assertCatalogViewDom(page, "customer", customerPayload.tenantId);
  VISIBLE_TYPES.customer.forEach((item) => allVisibleTypes.add(item));

  const customerRoot = await page.locator("[data-adapter-spec-root]").first().elementHandle();
  const [migrationResponse] = await Promise.all([
    waitForCatalogResponse(page, config, "migration"),
    page.locator('[data-adapter-spec-view="migration"]').click(),
  ]);
  const migrationPayload = await readCatalogResponse(migrationResponse, "migration");
  await waitForCatalogRootReplacement(page, customerRoot, "browser.migration");
  const migrationDom = await assertCatalogViewDom(page, "migration", migrationPayload.tenantId);
  VISIBLE_TYPES.migration.forEach((item) => allVisibleTypes.add(item));
  requireCondition(allVisibleTypes.size === 9, "browser.catalog_dom", "nine_types");
  screenshots.push(await captureViewport(page, config, "migration", VIEWPORTS[1]));
  await page.setViewportSize({ width: VIEWPORTS[0].width, height: VIEWPORTS[0].height });
  return {
    screenshots,
    read_entry_counts: [
      defaultPayload.summary.entry_count,
      customerPayload.summary.entry_count,
      migrationPayload.summary.entry_count,
    ],
    visible_entry_counts: [
      defaultDom.visible_entry_count,
      customerDom.visible_entry_count,
      migrationDom.visible_entry_count,
    ],
    all_nine_types_visible: true,
    migration_boundary_visible: true,
  };
}


/** 扫描 URL、DOM 与 Storage；认证 Token 只允许位于专用 Session 槽。 */
async function assertBrowserPrivacy(page, config) {
  const metrics = await page.evaluate(({ password, canary, authKeys }) => {
    const html = document.documentElement.outerHTML;
    const url = location.href;
    const rootText = document.querySelector("[data-adapter-spec-root]")?.textContent || "";
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
      token_outside_auth_slot: Boolean(token) && [
        url,
        html,
        JSON.stringify(localEntries),
        JSON.stringify(nonTokenSessionEntries),
      ].some((value) => value.includes(token)),
      bearer_hit_count: [url, html, allStorage].reduce(
        (count, value) => count + (value.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
        0,
      ),
      callable_url_text_count: (rootText.match(/https?:\/\//gi) ?? []).length,
    };
  }, {
    password: config.adminPassword,
    canary: config.sensitiveCanary,
    authKeys: AUTH_STORAGE_KEYS,
  });
  requireCondition(metrics.auth_slot_count === AUTH_STORAGE_KEYS.length, "browser.privacy", "auth_slots");
  requireCondition(metrics.unknown_session_slot_count === 0, "browser.privacy", "unknown_session_slot");
  requireCondition(metrics.forbidden_storage_key_count === 0, "browser.privacy", "storage_key");
  requireCondition(metrics.supplied_secret_hit_count === 0, "browser.privacy", "secret_exposed");
  requireCondition(!metrics.token_outside_auth_slot, "browser.privacy", "token_exposed");
  requireCondition(metrics.bearer_hit_count === 0, "browser.privacy", "bearer_exposed");
  requireCondition(metrics.callable_url_text_count === 0, "browser.privacy", "callable_url_exposed");
  // 只返回固定布尔结论和零命中计数，原始 DOM、Storage 与敏感输入不得进入最终证据。
  return { auth_storage_contract_valid: true, sensitive_url_dom_storage_hits: 0 };
}


/** 执行真实注销，并确认认证槽位和 PAGE-090 受保护 DOM 已销毁。 */
async function logoutAndVerify(page, config) {
  const [response] = await Promise.all([
    page.waitForResponse((candidate) => {
      try {
        const target = new URL(candidate.url());
        return target.origin === config.controlOrigin
          && target.pathname === "/auth/logout"
          && candidate.request().method() === "POST";
      } catch {
        return false;
      }
    }, { timeout: 30000 }),
    page.locator("#logout-button").click(),
  ]);
  requireCondition(response.status() === 200, "browser.logout", "status");
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30000 });
  const cleared = await page.evaluate((authKeys) => ({
    auth_slot_count: authKeys.filter((key) => sessionStorage.getItem(key) !== null).length,
    protected_dom_count: document.querySelectorAll(
      "[data-adapter-spec-root],.adapter-spec-page,.console-shell",
    ).length,
  }), AUTH_STORAGE_KEYS);
  requireCondition(cleared.auth_slot_count === 0, "browser.logout", "auth_slots");
  requireCondition(cleared.protected_dom_count === 0, "browser.logout", "protected_dom");
  // 返回稳定验收结论，避免把认证槽位内容或受保护 DOM 片段写入报告。
  return { auth_storage_cleared: true, protected_dom_cleared: true };
}


/** 深度验证最终证据只含汇总结论，不含敏感键、敏感原值或 Bearer 文本。 */
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
  requireCondition(!/Bearer\s+/i.test(serialized), "evidence.privacy", "bearer_value");
  for (const secret of secrets.filter(Boolean)) {
    requireCondition(!serialized.includes(secret), "evidence.privacy", "secret_value");
  }
  return evidence;
}


/** 使用 wx 独占发布脱敏 JSON，防止并发执行覆盖同一批次证据。 */
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


/** 启动离线 Chromium，完成管理员登录、三分类、双视口、隐私和注销验收。 */
async function executeAcceptance(config) {
  // Playwright 模块加载和 Browser 启动前同时固定父子进程目录，避免任何 profile 回退系统临时区。
  pinBrowserWritableRoots(process.env, config.artifactDirectory);
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
    const tracker = await runBrowserOperation(
      "browser.guards",
      () => installBrowserGuards(context, page, config),
    );
    const identity = await runBrowserOperation(
      "browser.login",
      () => loginAdministrator(page, config, tracker),
    );
    const catalog = await runBrowserOperation(
      "browser.catalog",
      () => exerciseCatalogViews(page, config),
    );
    const privacy = await runBrowserOperation("browser.privacy", () => assertBrowserPrivacy(page, config));
    requireCondition(tracker.blockedRequestCount === 0, "browser.network", "external_request");
    requireCondition(tracker.invalidCatalogRequestCount === 0, "browser.network", "catalog_contract");
    requireCondition(tracker.pageErrorCount === 0, "browser.errors", "page_error");
    requireCondition(tracker.consoleErrorCount === 0, "browser.errors", "console_error");
    requireCondition(
      ["default", "customer", "migration"].every((view) => tracker.catalogRequestCounts[view] > 0),
      "browser.catalog",
      "request_missing",
    );
    requireCondition(
      tracker.catalogStatuses.length >= 3 && tracker.catalogStatuses.every((status) => status === 200),
      "browser.catalog",
      "non_200_status",
    );
    const logout = await runBrowserOperation("browser.logout", () => logoutAndVerify(page, config));
    requireCondition(catalog.screenshots.length === 2, "browser.screenshots", "count");
    requireCondition(catalog.screenshots.every((item) => item.non_blank_pixels), "browser.screenshots", "blank");
    const evidence = {
      schema_version: 1,
      prd_id: "PRD-063",
      page_id: "PAGE-090",
      status: "passed",
      checks: {
        http_statuses: {
          login_200: tracker.loginStatuses.includes(200),
          identity_200: tracker.meStatuses.includes(200),
          navigation_200: tracker.navigationStatuses.includes(200),
          catalog_reads_200: true,
          logout_200: tracker.logoutStatuses.includes(200),
        },
        administrator_confirmed: identity.administrator_confirmed,
        catalog: {
          version_confirmed: true,
          hash_confirmed: true,
          read_entry_counts: catalog.read_entry_counts,
          visible_entry_counts: catalog.visible_entry_counts,
          all_nine_types_visible: catalog.all_nine_types_visible,
          migration_boundary_visible: catalog.migration_boundary_visible,
        },
        privacy,
        logout,
        network: { external_request_count: 0, invalid_catalog_request_count: 0 },
        browser_errors: { page_error_count: 0, console_error_count: 0 },
        screenshots: { count: 2, all_non_blank: true },
      },
      screenshots: catalog.screenshots,
    };
    assertSafeEvidence(evidence, [config.adminPassword, config.sensitiveCanary]);
    await publishEvidence(config.artifactDirectory, evidence);
    return evidence;
  } finally {
    await context?.close().catch(() => undefined);
    await browser?.close().catch(() => undefined);
  }
}


/** CLI 仅输出固定成功摘要或脱敏 stage/code，证据路径、口令和 Token 永不进入终端。 */
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
  CATALOG_HASH,
  CATALOG_VERSION,
  VIEWPORTS,
  adapterSpecRequestView,
  assertLayoutMetrics,
  assertSafeEvidence,
  browserRequestAllowed,
  buildBrowserEnvironment,
  classifyBrowserConsoleMessage,
  hasExactSearchParams,
  normalizeLoopbackOrigin,
  pinBrowserWritableRoots,
  resolveArtifactDirectory,
  validateAdapterCatalogPayload,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
