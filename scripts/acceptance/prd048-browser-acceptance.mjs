/**
 * PRD-048 PAGE-068/PAGE-069/调用日志真实 Chrome 验收。
 *
 * 本脚本只消费 Runtime 验收保留的租户内事实。网络、页面、截图和注销都采用失败关闭策略；
 * 密码、Session Token、Provider Key、Prompt 与 Response 正文只允许驻留进程内存，不得进入制品。
 */

import assert from "node:assert/strict";
import { statSync } from "node:fs";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { inspectPngPixels } from "./prd047-browser-acceptance.mjs";

const require = createRequire(import.meta.url);

// 服务地址固定到本机三个约定端口；环境变量只能改变回环主机写法，不能扩大浏览器网络范围。
const CONTROL_API_INPUT = new URL(process.env.METAHUB_CONTROL_API_URL ?? "http://localhost:8001");
const CONSOLE_INPUT = new URL(process.env.METAHUB_CONSOLE_URL ?? "http://localhost:5173");
const GATEWAY_INPUT = new URL(process.env.METAHUB_GATEWAY_URL ?? "http://localhost:8000");
const CONTROL_API_URL = CONTROL_API_INPUT.origin;
const CONSOLE_URL = CONSOLE_INPUT.origin;
const GATEWAY_URL = GATEWAY_INPUT.origin;
const TENANT_SLUG = (process.env.METAHUB_TENANT_SLUG ?? "default").trim();
const CHROME_PATH = process.env.METAHUB_CHROME_PATH
  ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const RUNTIME_STATE_PATH = path.resolve(String(process.env.METAHUB_PRD048_RUNTIME_STATE ?? ""));
const ARTIFACT_DIR = path.resolve(
  process.env.METAHUB_PRD048_BROWSER_ARTIFACT_DIR ?? "artifacts/prd048/browser",
);
const RESULT_PATH = path.join(ARTIFACT_DIR, "browser-result.json");

// 认证槽位、角色页面和视口是浏览器验收合同；后端权限仍由真实 403/422 探针独立证明。
const AUTH_TOKEN_KEY = "metahub.auth_token";
const AUTH_CONTEXT_KEY = "metahub.auth_context";
const AUTH_STORAGE_KEYS = new Set([AUTH_TOKEN_KEY, AUTH_CONTEXT_KEY]);
const TARGET_NAVIGATIONS = Object.freeze([
  "model-aliases",
  "routing-policies",
  "model-quality-scores",
  "request-logs",
]);
// 三档视口覆盖桌面、中间宽度和移动端，截图尺寸必须与这些稳定值逐一匹配。
const VIEWPORTS = Object.freeze([
  { name: "desktop-1280x720", width: 1280, height: 720 },
  { name: "intermediate-900x800", width: 900, height: 800 },
  { name: "mobile-390x844", width: 390, height: 844 },
]);

// Gateway 只开放无业务正文的健康/版本读取；任何其他 localhost 端口或路径都必须被硬阻断。
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);
const GATEWAY_HEALTH_PATHS = new Set(["/health", "/health/live", "/health/ready", "/version"]);
const INTERNAL_BROWSER_PROTOCOLS = new Set(["about:", "blob:", "data:"]);
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9_-]{1,160}$/;
const RUNTIME_SUFFIX_PATTERN = /^[a-z0-9]{8,32}$/;
const RBAC_PROBE_REQUEST_ID_PATTERN = /^req_prd048_browser_rbac_probe_(admin|developer)_/;
const SAFE_STAGE_PATTERN = /^[a-z0-9_.-]{1,120}$/;
const SAFE_FAILURE_CODE_PATTERN = /^[a-z0-9_]{1,80}$/;

// 精确 Secret 之外仍扫描常见 Key、Webhook、PRD 哨兵和 Bearer 形态，防止漏传环境变量时失守。
const GENERIC_SECRET_PATTERNS = Object.freeze([
  /\bBearer\s+[A-Za-z0-9._~+/-]{8,}/i,
  /\bmhv_[A-Za-z0-9_-]{9,}/,
  /\bsk[-_][A-Za-z0-9_-]{10,}/i,
  /\bwhsec_[A-Za-z0-9_-]{7,}/i,
  /prd048-(?:prompt|response)-/i,
  /Prd048!/,
]);
// 仅凭环境变量名称识别额外凭据，绝不使用 Secret 原值生成标签或错误信息。
const SENSITIVE_ENV_NAME = /(?:PASSWORD|TOKEN|SECRET|API_KEY|PROVIDER_KEY|VIRTUAL_KEY|PROMPT|RESPONSE)/i;

/**
 * 浏览器验收的脱敏阶段错误。
 * stage/code 只能使用固定白名单字符，message 不接收底层异常文本、URL、响应正文或凭据。
 */
class BrowserAcceptanceStageError extends Error {
  /** 用固定阶段与错误码构造可审计失败，两个值都会先经过严格字符白名单。 */
  constructor(stage, code) {
    const safeStage = SAFE_STAGE_PATTERN.test(String(stage)) ? String(stage) : "browser.unknown";
    const safeCode = SAFE_FAILURE_CODE_PATTERN.test(String(code)) ? String(code) : "stage_failed";
    super(`browser_acceptance_failed stage=${safeStage} code=${safeCode}`);
    this.name = "BrowserAcceptanceStageError";
    this.stage = safeStage;
    this.code = safeCode;
  }
}

/**
 * 把任意底层异常转换为固定 stage/code。
 * 已脱敏阶段错误保持原样；其他异常仅按 name 分类，不复制 message、stack、cause 或响应对象。
 */
function toSafeStageError(error, stage, fallbackCode = "stage_failed") {
  if (error instanceof BrowserAcceptanceStageError) return error;
  const name = String(error?.name ?? "Error");
  let code = fallbackCode;
  // 只按标准异常类名折叠断言与超时，绝不读取可能携带页面或响应内容的错误 message。
  if (name === "AssertionError") code = "assertion_failed";
  else if (name === "TimeoutError") code = "timeout";
  return new BrowserAcceptanceStageError(stage, code);
}

/** 执行一个命名验收阶段，失败时只向外传播白名单 stage/code。 */
async function runAcceptanceStage(stage, action, fallbackCode = "stage_failed") {
  try {
    return await action();
  } catch (error) {
    // 原始异常可能包含 locator、URL 查询或响应片段，必须在离开当前阶段前完成脱敏收敛。
    throw toSafeStageError(error, stage, fallbackCode);
  }
}

/** 把 URL.hostname 的 IPv6 方括号去掉，统一判断三个允许的回环主机。 */
function normalizedHostname(target) {
  return String(target.hostname).replace(/^\[|\]$/g, "").toLowerCase();
}

/**
 * 校验服务根地址的协议、端口和无凭据边界。
 * 参数是 URL、固定端口和标签；返回规范 origin，路径、查询或内嵌账号均按配置错误拒绝。
 */
function validateServiceRoot(target, expectedPort, label) {
  assert.ok(
    target.protocol === "http:"
      && LOOPBACK_HOSTS.has(normalizedHostname(target))
      && target.port === expectedPort
      && target.username === ""
      && target.password === ""
      && (target.pathname === "/" || target.pathname === "")
      && target.search === ""
      && target.hash === "",
    `${label} 必须是无凭据的固定端口回环服务根地址`,
  );
  return target.origin;
}

/**
 * 校验命令行环境和制品路径均属于 PRD-048 本机验收范围。
 * 路径约束避免误读其他批次状态，固定端口约束避免把任意 localhost 服务误当作受信端点。
 */
function assertLocalScope() {
  assert.equal(process.env.METAHUB_CONFIRM_BROWSER_STATE_MUTATION, "1");
  assert.ok(statSync(CHROME_PATH).isFile(), "Chrome 可执行文件不存在");
  assert.ok(statSync(RUNTIME_STATE_PATH).isFile(), "PRD-048 runtime-state 不存在");
  const normalizedRuntime = RUNTIME_STATE_PATH.toLowerCase();
  const normalizedArtifact = ARTIFACT_DIR.toLowerCase();
  assert.ok(normalizedRuntime.includes(`${path.sep}artifacts${path.sep}prd048${path.sep}`));
  assert.ok(normalizedArtifact.includes(`${path.sep}artifacts${path.sep}prd048${path.sep}`));
  validateServiceRoot(CONTROL_API_INPUT, "8001", "Control API");
  validateServiceRoot(CONSOLE_INPUT, "5173", "Console");
  validateServiceRoot(GATEWAY_INPUT, "8000", "Gateway");
}

/** 校验 Runtime 状态中的稳定标识符，失败信息只报告字段类别而不回显原值。 */
function requiredRuntimeId(value, label) {
  assert.ok(RESOURCE_ID_PATTERN.test(String(value ?? "")), `${label} 标识符非法`);
  return String(value);
}

/**
 * 解析并校验 Runtime 状态生命周期。
 * 只有 PRD-048、已通过且明确保留给浏览器、尚未清理的批次可被消费，防止复用过期资源。
 */
function validateRuntimeStateDocument(body, { env = process.env } = {}) {
  assert.ok(body && typeof body === "object" && !Array.isArray(body), "runtime-state 必须是对象");
  assert.equal(body.prd_id, "PRD-048", "runtime-state PRD 不匹配");
  assert.equal(body.status, "passed", "Runtime 验收尚未通过");
  assert.equal(body.lifecycle?.retained_for_browser, true, "Runtime 资源未保留给浏览器");
  assert.equal(body.lifecycle?.cleanup_completed, false, "Runtime 资源已经清理");
  const suffix = String(body.suffix ?? "");
  assert.ok(RUNTIME_SUFFIX_PATTERN.test(suffix), "Runtime suffix 非法");
  const ids = body.resource_ids ?? {};
  const requestIds = body.request_ids ?? {};
  const aliasName = requiredRuntimeId(ids.alias_name, "aliasName");
  assert.equal(aliasName, `prd048-runtime-${suffix}`, "Runtime Alias 与批次后缀不一致");
  const runtime = {
    suffix,
    tenantId: requiredRuntimeId(ids.main_tenant_id, "mainTenantId"),
    aliasId: requiredRuntimeId(ids.alias_id, "aliasId"),
    aliasName,
    modelAId: requiredRuntimeId(ids.model_a_id, "modelAId"),
    modelBId: requiredRuntimeId(ids.model_b_id, "modelBId"),
    qualityRequestId: requiredRuntimeId(requestIds.quality_wins, "qualityRequestId"),
    accounts: {
      admin: {
        email: String(env.METAHUB_ADMIN_EMAIL ?? "admin@metahub.local").trim(),
        password: String(env.METAHUB_PRD048_ADMIN_PASSWORD ?? ""),
        userId: requiredRuntimeId(ids.admin_user_id, "adminUserId"),
      },
      developer: {
        email: `prd048-developer-${suffix}@metahub.local`,
        password: String(env.METAHUB_PRD048_DEVELOPER_PASSWORD ?? ""),
        userId: requiredRuntimeId(ids.developer_user_id, "developerUserId"),
      },
      viewer: {
        email: `prd048-viewer-${suffix}@metahub.local`,
        password: String(env.METAHUB_PRD048_VIEWER_PASSWORD ?? ""),
        userId: requiredRuntimeId(ids.viewer_user_id, "viewerUserId"),
      },
    },
  };
  for (const [role, account] of Object.entries(runtime.accounts)) {
    // 三角色密码必须由进程环境提供，Runtime 状态文件不得承担凭据分发职责。
    assert.ok(account.email && account.password.length >= 8, `${role} 浏览器凭据缺失`);
  }
  return runtime;
}

/** 读取 UTF-8/BOM Runtime JSON，并交给严格生命周期解析器生成浏览器所需的最小对象。 */
async function loadRuntimeState(filePath = RUNTIME_STATE_PATH, env = process.env) {
  const source = (await readFile(filePath, "utf8")).replace(/^\uFEFF/, "");
  return validateRuntimeStateDocument(JSON.parse(source), { env });
}

/**
 * 收集进程内 Secret，不读取 Runtime 状态未公开字段。
 * 三角色密码始终加入；名称表明是密码、Token、Key、Secret 或正文哨兵的额外环境值也加入扫描。
 */
function collectConfiguredSecrets(runtime, env = process.env) {
  const secrets = new Set(
    Object.values(runtime.accounts).map((account) => account.password).filter((value) => value.length >= 4),
  );
  for (const [name, value] of Object.entries(env)) {
    // 环境名而非值决定是否属于凭据，避免把未知 Secret 本身用于错误信息或分类标签。
    if (SENSITIVE_ENV_NAME.test(name) && String(value ?? "").length >= 4) {
      secrets.add(String(value));
    }
  }
  // 返回 Set 供后续追加三个 UI Session Token；该集合本身从不参与结果序列化。
  return secrets;
}

/** 判断字符串是否出现精确 Secret 或常见凭据/正文哨兵，始终只返回布尔值。 */
function containsSecret(value, exactSecrets = []) {
  const text = String(value ?? "");
  const secrets = [...exactSecrets].map(String).filter((item) => item.length >= 4);
  // 精确值和通用形态任一命中即失败，调用方只获得布尔结论而不会得到匹配内容。
  return secrets.some((secret) => text.includes(secret))
    || GENERIC_SECRET_PATTERNS.some((pattern) => pattern.test(text));
}

/**
 * 对 DOM、URL 和 Storage 快照执行纯函数隐私校验。
 * 登录态仅允许当前 Token 出现在固定 sessionStorage 槽位，注销态必须清除两项认证状态。
 */
function assertSensitiveSurfaceSnapshot(
  snapshot,
  { exactSecrets = [], activeToken = null, loggedOut = false, label = "browser" } = {},
) {
  const localEntries = new Map(snapshot.localStorageEntries ?? []);
  const sessionEntries = new Map(snapshot.sessionStorageEntries ?? []);
  const publicValues = [
    snapshot.domText,
    snapshot.domHtml,
    snapshot.url,
    ...localEntries.keys(),
    ...localEntries.values(),
  ];
  assert.ok(
    publicValues.every((value) => !containsSecret(value, exactSecrets)),
    `${label} DOM/URL/localStorage 出现敏感信息`,
  );
  assert.ok(
    !localEntries.has(AUTH_TOKEN_KEY) && !localEntries.has(AUTH_CONTEXT_KEY),
    `${label} 禁止把认证态写入 localStorage`,
  );
  if (loggedOut) {
    // 注销必须同时清除 Token 和角色上下文，不能把半套认证状态留给后续角色上下文。
    assert.ok(
      !sessionEntries.has(AUTH_TOKEN_KEY) && !sessionEntries.has(AUTH_CONTEXT_KEY),
      `${label} 注销后仍保留认证槽位`,
    );
  } else {
    // 登录态 Token 必须与当前角色会话完全一致，布尔断言避免失败时输出实际 Token。
    assert.ok(typeof activeToken === "string" && activeToken.length >= 16, `${label} 活动 Token 缺失`);
    assert.ok(sessionEntries.get(AUTH_TOKEN_KEY) === activeToken, `${label} Token 槽位不匹配`);
    assert.ok(sessionEntries.has(AUTH_CONTEXT_KEY), `${label} 缺少认证上下文`);
  }
  for (const [key, value] of sessionEntries) {
    // 活跃 Token 只豁免唯一授权槽位；键名、认证上下文和其他值仍必须经过完整敏感扫描。
    if (!loggedOut && key === AUTH_TOKEN_KEY) continue;
    assert.ok(!containsSecret(key, exactSecrets), `${label} sessionStorage 键名出现敏感信息`);
    assert.ok(!containsSecret(value, exactSecrets), `${label} sessionStorage 出现敏感信息`);
  }
  const authKeyCount = [...sessionEntries.keys()].filter((key) => AUTH_STORAGE_KEYS.has(key)).length;
  assert.equal(authKeyCount, loggedOut ? 0 : 2, `${label} 认证槽位数量异常`);
  return {
    domSensitiveHitCount: 0,
    storageSensitiveHitCount: 0,
    authStorageContractValid: true,
  };
}

/** 从真实页面提取只驻留内存的 DOM、URL 与 Storage 快照。 */
async function readSensitiveSurfaceSnapshot(page) {
  return page.evaluate(() => ({
    domText: document.documentElement?.innerText ?? "",
    domHtml: document.documentElement?.outerHTML ?? "",
    url: window.location.href,
    localStorageEntries: Object.entries(window.localStorage),
    sessionStorageEntries: Object.entries(window.sessionStorage),
  }));
}

/**
 * 识别 PRD-048 目标 API 类别。
 * 返回值只含固定枚举，不保留 URL、查询、模型 ID 或调用日志正文，便于安全写入验收结果。
 */
function classifyTargetApi(rawUrl, controlOrigin = CONTROL_API_URL) {
  let target;
  try {
    target = new URL(String(rawUrl));
  } catch {
    return null;
  }
  if (target.origin !== new URL(controlOrigin).origin) return null;
  // ModelAlias 列表同时承载 PAGE-068 路由策略读取，写接口则单独归入 routing_policy。
  if (target.pathname === "/model-aliases") return "model_aliases";
  if (/^\/model-aliases\/[^/]+\/routing-mode$/.test(target.pathname)) return "routing_policy";
  if (target.pathname === "/model-quality-scores") return "quality_score";
  if (target.pathname === "/request-logs" || /^\/request-logs\/[^/]+$/.test(target.pathname)) {
    return "request_log";
  }
  return null;
}

/** 把请求头键统一转为小写；值只用于门禁判断，不进入网络证据。 */
function normalizedHeaders(headers = {}) {
  return Object.fromEntries(
    Object.entries(headers).map(([name, value]) => [String(name).toLowerCase(), String(value)]),
  );
}

/**
 * 判断 Console error 是否只是受控 RBAC 探针的 Chrome 资源状态提示。
 * 只有窗口开启且文本中的 403/422 与当前角色预期完全一致才忽略；5xx 和其他错误始终返回 false。
 */
function isExpectedProbeConsoleError(messageType, messageText, expectedStatus = null) {
  if (messageType !== "error" || ![403, 422].includes(expectedStatus)) return false;
  const match = String(messageText ?? "").match(
    /^Failed to load resource: the server responded with a status of (403|422)(?:\s|$)/,
  );
  return Number(match?.[1] ?? 0) === expectedStatus;
}

/**
 * 核对被抑制的 Chrome 探针提示没有超过已确认的探针响应数。
 * 不要求浏览器一定生成提示，但多余提示意味着窗口错配，必须以固定错误码失败。
 */
function assertExpectedProbeConsoleCount(observedCount, confirmedResponseCount, stage) {
  if (
    !Number.isInteger(observedCount)
      || !Number.isInteger(confirmedResponseCount)
      || observedCount < 0
      || confirmedResponseCount < 0
      || observedCount > confirmedResponseCount
  ) {
    // 越界意味着豁免数量无法由已确认探针解释，必须固定失败而不能清空历史浏览器错误。
    throw new BrowserAcceptanceStageError(stage, "expected_probe_console_count_invalid");
  }
  return observedCount;
}

/**
 * 纯函数判定单个浏览器请求是否允许。
 * Console/Control 必须精确 origin；Gateway 只允许四个无查询健康路径的 GET 和最小 CORS 预检。
 */
function decideBrowserNetworkRequest(
  { url, method = "GET", headers = {} },
  {
    consoleOrigin = CONSOLE_URL,
    controlOrigin = CONTROL_API_URL,
    gatewayOrigin = GATEWAY_URL,
  } = {},
) {
  let target;
  try {
    target = new URL(String(url));
  } catch {
    return { allowed: false, destination: "blocked", violation: "invalid_url" };
  }
  if (INTERNAL_BROWSER_PROTOCOLS.has(target.protocol)) {
    return { allowed: true, destination: "browser_internal", violation: null };
  }
  const safeLoopback = target.protocol === "http:"
    && LOOPBACK_HOSTS.has(normalizedHostname(target))
    && target.username === ""
    && target.password === "";
  if (safeLoopback && target.origin === new URL(consoleOrigin).origin) {
    return { allowed: true, destination: "console", violation: null };
  }
  if (safeLoopback && target.origin === new URL(controlOrigin).origin) {
    return { allowed: true, destination: "control", violation: null };
  }
  const requestHeaders = normalizedHeaders(headers);
  const requestMethod = String(method).toUpperCase();
  const gatewayPathAllowed = GATEWAY_HEALTH_PATHS.has(target.pathname)
    && target.search === ""
    && target.hash === "";
  const sensitiveGatewayHeader = ["authorization", "cookie", "x-internal-token"]
    .some((name) => Object.hasOwn(requestHeaders, name));
  const gatewayGetAllowed = requestMethod === "GET"
    && requestHeaders.origin === new URL(consoleOrigin).origin
    && !sensitiveGatewayHeader;
  const requestedHeaders = String(requestHeaders["access-control-request-headers"] ?? "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .sort();
  const gatewayPreflightAllowed = requestMethod === "OPTIONS"
    && requestHeaders.origin === new URL(consoleOrigin).origin
    && String(requestHeaders["access-control-request-method"] ?? "").toUpperCase() === "GET"
    && requestedHeaders.length === 1
    && requestedHeaders[0] === "x-request-id"
    && !sensitiveGatewayHeader;
  if (
    safeLoopback
      && target.origin === new URL(gatewayOrigin).origin
      && gatewayPathAllowed
      && (gatewayGetAllowed || gatewayPreflightAllowed)
  ) {
    return { allowed: true, destination: "gateway_health", violation: null };
  }
  let violation = "origin";
  if (target.origin === new URL(gatewayOrigin).origin) {
    // Gateway 的路径、查询和方法/头边界分开记账，便于审计而不保存原始 URL 或 Header。
    if (!GATEWAY_HEALTH_PATHS.has(target.pathname)) violation = "gateway_path";
    else if (target.search !== "" || target.hash !== "") violation = "gateway_query";
    else violation = "gateway_method_or_headers";
  } else if (target.protocol !== "http:") {
    violation = "protocol";
  }
  return { allowed: false, destination: "blocked", violation };
}

/**
 * 安装 Playwright 请求硬门禁和脱敏元数据监听。
 * 证据只记录固定目标类别、方法、状态与布尔响应合同，不保存 URL 查询、Header 值或正文。
 */
async function installBrowserGuards(
  context,
  page,
  {
    consoleOrigin = CONSOLE_URL,
    controlOrigin = CONTROL_API_URL,
    gatewayOrigin = GATEWAY_URL,
  } = {},
) {
  const evidence = {
    requestCount: 0,
    blockedRequestCount: 0,
    violations: [],
    browserErrors: [],
    networkMetadata: [],
    targetRequests: [],
    targetResponses: [],
    checkpointCount: 0,
    expectedProbeConsoleStatus: null,
    expectedProbeConsoleErrorCount: 0,
  };
  const requestSequence = new WeakMap();
  let nextSequence = 0;
  context.on("request", (request) => {
    const raw = request.url();
    let target;
    try {
      target = new URL(raw);
    } catch {
      target = null;
    }
    if (target && INTERNAL_BROWSER_PROTOCOLS.has(target.protocol)) return;
    const sequence = ++nextSequence;
    requestSequence.set(request, sequence);
    evidence.requestCount += 1;
    const surface = classifyTargetApi(raw, controlOrigin);
    let destination = "blocked";
    if (target?.origin === new URL(consoleOrigin).origin) destination = "console";
    else if (target?.origin === new URL(controlOrigin).origin) destination = "control";
    else if (target?.origin === new URL(gatewayOrigin).origin) destination = "gateway";
    evidence.networkMetadata.push({
      sequence,
      method: request.method(),
      destination,
      targetSurface: surface,
    });
    if (surface) {
      const requestId = normalizedHeaders(request.headers())["x-request-id"] ?? "";
      evidence.targetRequests.push({
        sequence,
        method: request.method(),
        surface,
        probe: RBAC_PROBE_REQUEST_ID_PATTERN.test(requestId),
      });
    }
  });
  await context.route("**/*", async (route) => {
    const request = route.request();
    const decision = decideBrowserNetworkRequest(
      {
        url: request.url(),
        method: request.method(),
        headers: await request.allHeaders(),
      },
      { consoleOrigin, controlOrigin, gatewayOrigin },
    );
    if (decision.allowed) {
      await route.continue();
      return;
    }
    // 被拒绝的请求只留下固定违规类别；原始 URL 可能含凭据或业务查询，绝不能写入证据。
    evidence.blockedRequestCount += 1;
    evidence.violations.push(decision.violation);
    await route.abort("blockedbyclient");
  });
  page.on("pageerror", () => evidence.browserErrors.push("pageerror"));
  page.on("console", (message) => {
    // Console 文本可能复制响应或用户输入，错误时只保存类别和计数。
    if (message.type() === "error") {
      if (isExpectedProbeConsoleError(
        message.type(),
        message.text(),
        evidence.expectedProbeConsoleStatus,
      )) {
        // 受控 403/422 已由状态码和固定错误码断言；这里只计数，不把 Chrome 文案写入结果。
        evidence.expectedProbeConsoleErrorCount += 1;
        return;
      }
      evidence.browserErrors.push("console_error");
    }
  });
  page.on("response", (response) => {
    const surface = classifyTargetApi(response.url(), controlOrigin);
    // 任何 5xx 都意味着真实页面主流程不稳定，只记录固定类别并在下一个检查点失败。
    if (response.status() >= 500) evidence.browserErrors.push("http_5xx");
    if (!surface) return;
    evidence.targetResponses.push({
      sequence: requestSequence.get(response.request()) ?? 0,
      method: response.request().method(),
      surface,
      status: response.status(),
      noStore: String(response.headers()["cache-control"] ?? "").toLowerCase().includes("no-store"),
      requestIdPresent: Boolean(response.headers()["x-request-id"]),
      traceIdPresent: Boolean(response.headers()["x-trace-id"]),
    });
  });
  return evidence;
}

/** 校验网络元数据本身没有携带 Secret，并确认当前检查点之前没有任何门禁或浏览器错误。 */
function assertNetworkEvidencePrivacy(evidence, exactSecrets = [], label = "browser") {
  assert.ok(
    !containsSecret(JSON.stringify(evidence.networkMetadata ?? []), exactSecrets)
      && !containsSecret(JSON.stringify(evidence.targetRequests ?? []), exactSecrets)
      && !containsSecret(JSON.stringify(evidence.targetResponses ?? []), exactSecrets),
    `${label} 网络元数据出现敏感信息`,
  );
  assert.equal(evidence.blockedRequestCount ?? 0, 0, `${label} 出现被阻断网络请求`);
  assert.deepEqual(evidence.violations ?? [], [], `${label} 网络门禁违规`);
  assert.deepEqual(evidence.browserErrors ?? [], [], `${label} 浏览器错误`);
  return { networkSensitiveHitCount: 0, blockedRequestCount: 0, browserErrorCount: 0 };
}

/**
 * 在页面或截图边界执行 DOM、URL、Storage 与网络元数据联合扫描。
 * 每次成功才递增计数，失败检查点不会被最终结果误记为已通过。
 */
async function scanBrowserCheckpoint(
  page,
  evidence,
  exactSecrets,
  { activeToken = null, loggedOut = false, label = "browser" } = {},
) {
  const snapshot = await readSensitiveSurfaceSnapshot(page);
  const surface = assertSensitiveSurfaceSnapshot(snapshot, {
    exactSecrets,
    activeToken,
    loggedOut,
    label,
  });
  const network = assertNetworkEvidencePrivacy(evidence, exactSecrets, label);
  evidence.checkpointCount += 1;
  return { ...surface, ...network };
}

/** 使用真实登录表单建立 UI Session，并核对 /auth/login 与 /auth/me 的租户、用户和角色。 */
async function login(page, runtime, role, requestedNavigation, onSession) {
  const account = runtime.accounts[role];
  await page.goto(`${CONSOLE_URL}/#${requestedNavigation}`, { waitUntil: "domcontentloaded" });
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30_000 });
  await page.locator('[name="tenantSlug"]').fill(TENANT_SLUG);
  await page.locator('[name="email"]').fill(account.email);
  await page.locator('[name="password"]').fill(account.password);
  const loginResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === "/auth/login"
      && response.request().method() === "POST";
  }, { timeout: 30_000 });
  const meResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === "/auth/me"
      && response.request().method() === "GET";
  }, { timeout: 30_000 });
  await page.locator('#login-form button[type="submit"]').click();
  const loginResponse = await loginResponsePromise;
  const loginBody = await loginResponse.json();
  assert.equal(loginResponse.status(), 200, "登录接口未返回 200");
  const session = {
    token: String(loginBody.access_token ?? ""),
    sessionId: String(loginBody.session_id ?? ""),
  };
  // 登录响应一旦创建 Session 就先登记到外层，确保后续 owner 断言失败时 finally 仍执行严格注销。
  onSession(session);
  assert.ok(session.token.length >= 16 && session.sessionId, "登录响应缺少 Session 合同");
  assert.ok(loginBody.user?.tenant_id === runtime.tenantId, "登录租户不匹配");
  assert.ok(loginBody.user?.user_id === account.userId, "登录用户不匹配");
  assert.ok(loginBody.user?.role === role, "登录角色不匹配");
  assert.ok(loginBody.request_id && loginBody.trace_id, "登录响应缺少链路标识");
  assert.ok(loginResponse.headers()["x-request-id"] === loginBody.request_id, "登录 request_id 不一致");
  assert.ok(loginResponse.headers()["x-trace-id"] === loginBody.trace_id, "登录 trace_id 不一致");
  const meResponse = await meResponsePromise;
  const meBody = await meResponse.json();
  assert.equal(meResponse.status(), 200, "auth/me 未返回 200");
  assert.ok(meBody.user?.tenant_id === runtime.tenantId, "auth/me 租户不匹配");
  assert.ok(meBody.user?.user_id === account.userId, "auth/me 用户不匹配");
  assert.ok(meBody.user?.role === role, "auth/me 角色不匹配");
  assert.ok(meBody.session?.session_id === session.sessionId, "登录与 auth/me Session 不一致");
  await page.locator(".console-shell").waitFor({ state: "visible", timeout: 30_000 });
  // Session 只交给内存扫描和严格注销使用，调用方不得把该对象并入最终 JSON。
  return session;
}

/** 点击后端返回的导航项，并等待壳层与目标标题同时切换完成。 */
async function navigate(page, itemId, targetSelector) {
  const button = page.locator(`[data-console-nav="${itemId}"]`);
  await button.waitFor({ state: "visible", timeout: 30_000 });
  await button.click();
  await page.locator(`.console-shell[data-active-navigation="${itemId}"]`).waitFor({
    state: "visible",
    timeout: 30_000,
  });
  if (targetSelector) await page.locator(targetSelector).waitFor({ state: "visible", timeout: 30_000 });
  await page.waitForTimeout(250);
}

/** 验证页面没有横向溢出，且目标面板保持在当前视口内。 */
async function assertLayout(page, viewport, panelSelector) {
  const result = await page.evaluate(({ width, selector }) => {
    const root = document.documentElement;
    const body = document.body;
    const panel = selector ? document.querySelector(selector) : document.querySelector("main");
    const rect = panel?.getBoundingClientRect() ?? null;
    return {
      innerWidth: window.innerWidth,
      rootScrollWidth: root.scrollWidth,
      bodyScrollWidth: body.scrollWidth,
      panelLeft: rect?.left ?? 0,
      panelRight: rect?.right ?? 0,
      panelWidth: rect?.width ?? 0,
      requestedWidth: width,
    };
  }, { width: viewport.width, selector: panelSelector });
  assert.equal(result.innerWidth, viewport.width);
  assert.ok(result.rootScrollWidth <= viewport.width + 1, "document 出现横向溢出");
  assert.ok(result.bodyScrollWidth <= viewport.width + 1, "body 出现横向溢出");
  assert.ok(
    result.panelWidth > 0 && result.panelLeft >= -1 && result.panelRight <= viewport.width + 1,
    "目标面板越出视口",
  );
  return { horizontalOverflow: false, panelVisible: true };
}

/**
 * 在截图前后都执行联合隐私扫描，先把 PNG 留在内存，第二次扫描通过后才写盘。
 * 因此迟到网络或 DOM 变化不会产生未经验证的截图制品。
 */
async function capture(page, role, pageName, viewport, scan) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  await page.waitForTimeout(150);
  await scan(`${pageName}:${viewport.name}:screenshot_before`);
  const buffer = await page.screenshot({ fullPage: true });
  const pixels = inspectPngPixels(buffer, { expectedWidth: viewport.width, minHeight: viewport.height });
  await scan(`${pageName}:${viewport.name}:screenshot_after`);
  const filename = `${role}-${pageName}-${viewport.name}.png`;
  await writeFile(path.join(ARTIFACT_DIR, filename), buffer);
  return { filename, ...pixels, privacyScansPassed: 2 };
}

/** PAGE-068 核对质量成本模式、五字段以及 admin/developer 的结构化写边界。 */
async function verifyRoutingPolicyPage(page, runtime, role, scan) {
  await navigate(page, "routing-policies", "#routing-policies-title");
  await scan("routing_policy:page_before");
  const row = page.locator(`[data-routing-policy-row="${runtime.aliasId}"]`);
  await row.waitFor({ state: "visible", timeout: 30_000 });
  const text = await row.innerText();
  assert.ok(text.includes(runtime.aliasName));
  assert.match(text, /质量成本/);
  // 角色决定 DOM 写能力，但真正授权结论还必须由后面的后端 PATCH 探针给出。
  if (role === "admin") {
    // admin 必须获得真实表单；后续无副作用 PATCH 探针再证明后端权限，不能只依赖 DOM。
    const form = row.locator("[data-routing-policy-form]");
    assert.equal(await form.count(), 1);
    assert.equal(await form.locator('input[name="routingMode"][value="quality_cost"]').isChecked(), true);
    assert.equal(await form.locator('[name="qualityWeightPercent"]').inputValue(), "90");
    assert.equal(await form.locator('[name="costWeightPercent"]').inputValue(), "10");
    assert.equal(await form.locator('[name="latencyWeightPercent"]').inputValue(), "0");
    assert.equal(await form.locator('[name="qualityFloorPercent"]').inputValue(), "60");
    assert.equal(await form.locator('[name="minimumSampleCount"]').inputValue(), "20");
    assert.match(await form.locator("[data-routing-quality-total]").innerText(), /100%/);
  } else {
    // developer 只能看到冻结摘要，DOM 中不得生成可被手工启用的写表单。
    assert.equal(await row.locator("[data-routing-policy-form]").count(), 0);
    assert.match(text, /质量 90% \/ 成本 10% \/ 延迟 0%/);
  }
  await scan("routing_policy:page_after");
  return { rowVisible: true, writable: role === "admin", mode: "quality_cost" };
}

/** PAGE-069 核对两条模型评分、冻结统计以及 admin/developer 的结构化写边界。 */
async function verifyQualityScorePage(page, runtime, role, scan) {
  await navigate(page, "model-quality-scores", "#model-quality-scores-title");
  await scan("quality_score:page_before");
  const rowA = page.locator(`[data-model-quality-row="${runtime.modelAId}"]`);
  const rowB = page.locator(`[data-model-quality-row="${runtime.modelBId}"]`);
  await rowA.waitFor({ state: "visible", timeout: 30_000 });
  await rowB.waitFor({ state: "visible", timeout: 30_000 });
  assert.ok((await rowA.innerText()).includes(`prd048-runtime-a-${runtime.suffix}`));
  assert.ok((await rowB.innerText()).includes(`prd048-runtime-b-${runtime.suffix}`));
  // admin/developer 必须生成不同结构，不能只用 disabled 属性伪装权限隔离。
  if (role === "admin") {
    // admin 评分表单与后端 422 探针共同证明写权限确实到达领域校验层。
    assert.equal(await rowA.locator("[data-model-quality-form]").count(), 1);
    assert.equal(await rowB.locator("[data-model-quality-form]").count(), 1);
    assert.equal(await rowA.locator('[name="manualQualityPercent"]').inputValue(), "100");
    assert.equal(await rowB.locator('[name="manualQualityPercent"]').inputValue(), "65");
  } else {
    // developer 页面只读且后端 PATCH 必须返回 403，两层约束缺一不可。
    assert.equal(await page.locator("[data-model-quality-form]").count(), 0);
  }
  await scan("quality_score:page_after");
  return { rowsVisible: 2, writable: role === "admin" };
}

/** 冻结质量证据组必须恰好对应本轮两个路由候选，失败时返回固定断言码。 */
function assertRequestLogQualityEvidenceCount(count, stage = "request_log.verify") {
  if (count !== 2) {
    // ARIA 标签不属于 innerText；数量必须从语义 locator 读取，避免再次把可访问名称误当可见文案。
    throw new BrowserAcceptanceStageError(stage, "request_log_quality_evidence_missing");
  }
  return true;
}

/** 调用日志页按 Runtime request_id 定位详情并核对冻结质量解释。 */
async function verifyRequestLogPage(page, runtime, scan, stage = "request_log.verify") {
  await navigate(page, "request-logs", "#request-logs-title");
  await scan("request_log:page_before");
  const form = page.locator("#request-log-filter-form");
  await form.locator('[name="requestId"]').fill(runtime.qualityRequestId);
  const listResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === "/request-logs"
      && target.searchParams.get("request_id") === runtime.qualityRequestId;
  }, { timeout: 30_000 });
  await form.locator('button[type="submit"]').click();
  assert.equal((await listResponsePromise).status(), 200);
  const detailButton = page.locator(`[data-request-log-detail="${runtime.qualityRequestId}"]`);
  await detailButton.waitFor({ state: "visible", timeout: 30_000 });
  const detailResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === `/request-logs/${runtime.qualityRequestId}`;
  }, { timeout: 30_000 });
  await detailButton.click();
  assert.equal((await detailResponsePromise).status(), 200);
  const detail = page.locator(".request-log-detail");
  await detail.locator("h3").waitFor({ state: "visible", timeout: 30_000 });
  const text = await detail.innerText();
  assert.match(text, /quality_cost_v1/);
  assert.ok(text.includes(`prd048-runtime-a-${runtime.suffix}`));
  assert.match(text, /统计 v0/);
  const qualityEvidenceGroups = detail.locator(
    '.request-routing-quality[aria-label="冻结质量评分事实"]',
  );
  await qualityEvidenceGroups.first().waitFor({ state: "visible", timeout: 30_000 });
  assertRequestLogQualityEvidenceCount(await qualityEvidenceGroups.count(), stage);
  assert.match(text, /最终加权分/);
  await scan("request_log:page_after");
  // 返回值只保留冻结版本和公式标识，不复制调用日志详情、Prompt 或 Response。
  return { detailVisible: true, frozenStatisticsVersion: 0, formula: "quality_cost_v1" };
}

/**
 * 对路由策略和评分发送无副作用空对象 PATCH。
 * admin 必须到达领域校验返回 422，developer 必须在读取正文前由后端返回 403。
 */
async function verifyWriteBoundary(page, runtime, role, token, scan, evidence) {
  await scan("write_boundary:before");
  const expectedProbeStatus = role === "admin" ? 422 : 403;
  const initialProbeConsoleErrorCount = evidence.expectedProbeConsoleErrorCount;
  evidence.expectedProbeConsoleStatus = expectedProbeStatus;
  let probes;
  try {
    probes = await page.evaluate(async ({ controlUrl, aliasId, actorRole, authToken, suffix }) => {
      const endpoints = [
        {
          name: "routingPolicy",
          url: `${controlUrl}/model-aliases/${encodeURIComponent(aliasId)}/routing-mode`,
        },
        { name: "qualityScore", url: `${controlUrl}/model-quality-scores` },
      ];
      const outcomes = {};
      for (const endpoint of endpoints) {
        const requestId = `req_prd048_browser_rbac_probe_${actorRole}_${endpoint.name}_${suffix}`;
        const response = await fetch(endpoint.url, {
          method: "PATCH",
          headers: {
            Authorization: `Bearer ${authToken}`,
            "Content-Type": "application/json",
            "X-Request-ID": requestId,
          },
          body: "{}",
        });
        let payload = {};
        try {
          payload = await response.json();
        } catch {
          payload = {};
        }
        // 统一错误体的链路布尔值属于浏览器可见合同；响应头可见性只记证据，不替代后端 header 测试。
        outcomes[endpoint.name] = {
          status: response.status,
          code: typeof payload.code === "string" ? payload.code : "missing_code",
          requestIdPresent: typeof payload.request_id === "string" && payload.request_id.length > 0,
          traceIdPresent: typeof payload.trace_id === "string" && payload.trace_id.length > 0,
          requestIdHeaderVisible: Boolean(response.headers.get("X-Request-ID")),
          traceIdHeaderVisible: Boolean(response.headers.get("X-Trace-ID")),
        };
      }
      return outcomes;
    }, {
      controlUrl: CONTROL_API_URL,
      aliasId: runtime.aliasId,
      actorRole: role,
      authToken: token,
      suffix: runtime.suffix,
    });
    // fetch/json 完成后短暂保留窗口，让 Chrome 的对应资源状态提示先进入分类监听器。
    await page.waitForTimeout(100);
  } finally {
    // 探针窗口必须及时关闭，后续页面和注销产生的任何 console:error 仍按真实错误处理。
    evidence.expectedProbeConsoleStatus = null;
  }
  const probeConsoleErrorCount = (
    evidence.expectedProbeConsoleErrorCount - initialProbeConsoleErrorCount
  );
  const expected = role === "admin"
    ? {
      routingPolicy: [422, "model_alias.routing_mode_invalid"],
      qualityScore: [422, "model_quality_score.input_invalid"],
    }
    : {
      routingPolicy: [403, "auth.permission_denied"],
      qualityScore: [403, "auth.permission_denied"],
    };
  for (const name of ["routingPolicy", "qualityScore"]) {
    assert.equal(probes[name].status, expected[name][0], `${role} ${name} 写边界状态错误`);
    assert.equal(probes[name].code, expected[name][1], `${role} ${name} 写边界错误码错误`);
    assert.ok(probes[name].requestIdPresent && probes[name].traceIdPresent, `${role} ${name} 缺少链路标识`);
  }
  // 两个响应合同全部确认后，才允许消费至多两条对应 Chrome 资源提示；0 条也是合法浏览器行为。
  probes.expectedConsoleErrorCount = assertExpectedProbeConsoleCount(
    probeConsoleErrorCount,
    2,
    `${role}.write_boundary.verify`,
  );
  await scan("write_boundary:after");
  return probes;
}

/** 人为延迟 PAGE-069 GET，离页后释放响应并证明旧 owner 结果不能重新创建评分 DOM。 */
async function verifyLateQualityResponse(page) {
  await navigate(page, "overview", null);
  let startedResolve;
  let releaseResolve;
  const started = new Promise((resolve) => { startedResolve = resolve; });
  const release = new Promise((resolve) => { releaseResolve = resolve; });
  /** 延迟真实响应直到页面 owner 已切回 overview，响应正文不会进入验收结果。 */
  async function delayedHandler(route) {
    const response = await route.fetch();
    startedResolve();
    await release;
    await route.fulfill({ response }).catch(() => undefined);
  }
  await page.route(/\/model-quality-scores(?:\?.*)?$/, delayedHandler);
  try {
    await page.locator('[data-console-nav="model-quality-scores"]').click();
    await started;
    await page.locator('[data-console-nav="overview"]').click();
    releaseResolve();
    await page.locator('.console-shell[data-active-navigation="overview"]').waitFor({ timeout: 30_000 });
    await page.waitForTimeout(800);
    assert.equal(await page.locator(".model-quality-scores-panel").count(), 0);
    return { staleDomRejected: true };
  } finally {
    releaseResolve();
    await page.unroute(/\/model-quality-scores(?:\?.*)?$/, delayedHandler);
  }
}

/**
 * 纯函数核对角色目标 API 证据。
 * viewer 必须零请求；admin/developer 必须覆盖三类读取，且仅两条无副作用 PATCH 探针可写。
 */
function assertRoleTargetEvidence(role, evidence) {
  const requests = evidence.targetRequests ?? [];
  const responses = evidence.targetResponses ?? [];
  // viewer 权限由后端导航裁剪，因此连读取预取也不允许发生。
  if (role === "viewer") {
    // viewer 的导航由后端裁剪，任何目标 API 请求都表示前端发生了越权预取。
    assert.equal(requests.length, 0, "viewer 必须对目标 API 零预取");
    assert.equal(responses.length, 0, "viewer 不得接收目标 API 响应");
    return { targetRequestCount: 0, targetPrefetchCount: 0, writeProbeCount: 0 };
  }
  const readSurfaces = new Set(
    requests.filter((item) => item.method === "GET").map((item) => item.surface),
  );
  for (const surface of ["model_aliases", "quality_score", "request_log"]) {
    assert.ok(readSurfaces.has(surface), `${role} 未覆盖 ${surface} 读取`);
  }
  const writes = requests.filter((item) => item.method !== "GET");
  assert.equal(writes.length, 2, `${role} 写探针数量异常`);
  assert.deepEqual(
    [...writes.map((item) => item.surface)].sort(),
    ["quality_score", "routing_policy"],
    `${role} 写请求越出允许接口`,
  );
  assert.ok(writes.every((item) => item.method === "PATCH" && item.probe), `${role} 出现非探针写请求`);
  const writeResponses = responses.filter((item) => item.method === "PATCH");
  const expectedStatus = role === "admin" ? 422 : 403;
  assert.equal(writeResponses.length, 2, `${role} 写探针响应数量异常`);
  assert.ok(writeResponses.every((item) => item.status === expectedStatus), `${role} 写探针状态异常`);
  const successfulReads = responses.filter((item) => item.method === "GET" && item.status === 200);
  for (const surface of ["model_aliases", "quality_score", "request_log"]) {
    assert.ok(successfulReads.some((item) => item.surface === surface), `${role} ${surface} 缺少成功响应`);
  }
  assert.ok(
    successfulReads.every((item) => item.requestIdPresent && item.traceIdPresent),
    `${role} 目标 API 缺少 request_id/trace_id`,
  );
  // PRD-048 只为评分和调用日志定义禁止缓存合同；ModelAlias 是既有读取接口，不能由验收器擅自扩约。
  assert.ok(
    successfulReads
      .filter((item) => item.surface === "quality_score" || item.surface === "request_log")
      .every((item) => item.noStore),
    `${role} 敏感质量事实响应缺少 no-store`,
  );
  return {
    targetRequestCount: requests.length,
    targetPrefetchCount: 0,
    writeProbeCount: writes.length,
    writeProbeExpectedStatus: expectedStatus,
    readSurfaceCount: readSurfaces.size,
  };
}

/**
 * 执行最多两次严格注销尝试。
 * 只有观测到 200 且注销后 Storage 扫描通过才成功；失败审计只含状态枚举和次数。
 */
async function executeLogoutWithRetry({ attemptAction, verifyAction, maxAttempts = 2 }) {
  assert.ok(Number.isInteger(maxAttempts) && maxAttempts >= 1 && maxAttempts <= 3, "注销重试次数非法");
  const audit = { completed: false, attemptCount: 0, responseStatuses: [], authStorageCleared: false };
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    audit.attemptCount = attempt;
    let outcome;
    try {
      outcome = await attemptAction(attempt);
    } catch {
      // Playwright 原始异常可能携带 URL 或页面片段，审计只记固定类别并继续有限重试。
      audit.responseStatuses.push("attempt_error");
      continue;
    }
    const status = Number.isInteger(outcome?.status) ? outcome.status : null;
    audit.responseStatuses.push(status ?? "missing_response");
    if (status !== 200) continue;
    try {
      await verifyAction();
    } catch {
      // 后端 200 但页面或 Storage 未进入注销终态时不可重试伪装成功，必须严格失败。
      const error = new Error("注销响应成功但浏览器认证终态校验失败");
      error.audit = { ...audit };
      // 已观测到后端 200 却没有浏览器终态，继续点击会掩盖客户端清理缺陷，必须立即失败。
      throw error;
    }
    audit.completed = true;
    audit.authStorageCleared = true;
    return audit;
  }
  const error = new Error("注销在有限重试后仍未成功");
  error.audit = { ...audit };
  // 有限次数耗尽后固定失败，audit 只含次数和状态枚举，底层异常文本不会向外传播。
  throw error;
}

/** 点击真实注销按钮、等待后端响应，并在成功后联合复扫页面和网络元数据。 */
async function logoutStrict(page, evidence, exactSecrets) {
  return executeLogoutWithRetry({
    /** 每次尝试只返回 HTTP 状态，响应正文和异常文本都不得进入注销审计。 */
    async attemptAction() {
      const button = page.locator("#logout-button");
      if ((await button.count()) !== 1) return { status: null };
      const responsePromise = page.waitForResponse((response) => {
        const target = new URL(response.url());
        return target.origin === CONTROL_API_URL
          && target.pathname === "/auth/logout"
          && response.request().method() === "POST";
      }, { timeout: 10_000 }).catch(() => null);
      await button.click({ timeout: 10_000 });
      const response = await responsePromise;
      return { status: response?.status() ?? null };
    },
    /** 200 之后必须同时看到登录页、空认证 Storage 和无网络违规，才能确认注销完成。 */
    async verifyAction() {
      await page.locator("#login-form").waitFor({ state: "visible", timeout: 10_000 });
      await scanBrowserCheckpoint(page, evidence, exactSecrets, {
        activeToken: null,
        loggedOut: true,
        label: "logout",
      });
    },
  });
}

/**
 * 统一收口角色执行、严格注销和 Context 关闭。
 * 主流程失败时仍注销；注销失败不会被吞掉，多个失败以不含原始消息的 AggregateError 汇总。
 */
async function runRoleLifecycle({ stagePrefix = "browser", executeAction, logoutAction, closeAction }) {
  let result;
  let logoutAudit;
  const failures = [];
  try {
    result = await executeAction();
  } catch (error) {
    failures.push(toSafeStageError(error, `${stagePrefix}.page`, "page_failed"));
  }
  try {
    logoutAudit = await logoutAction();
  } catch (error) {
    // 注销失败必须独立进入最终失败集合，不能由更早的页面断言或 finally 路径吞掉。
    failures.push(toSafeStageError(error, `${stagePrefix}.logout`, "logout_failed"));
  }
  try {
    await closeAction();
  } catch (error) {
    failures.push(toSafeStageError(error, `${stagePrefix}.context_close`, "context_close_failed"));
  }
  // 页面、注销或 Context 任一失败都不能返回部分成功结果，且固定消息不会回显凭据。
  if (failures.length > 0) throw new AggregateError(failures, "浏览器角色生命周期失败");
  return { ...result, logout: logoutAudit };
}

/** 对 admin/developer 执行三页、三视口、后端写边界和迟到响应验收。 */
async function runReaderRole(browser, runtime, role, secretRegistry) {
  const context = await browser.newContext({ viewport: VIEWPORTS[0] });
  const page = await context.newPage();
  const guards = await installBrowserGuards(context, page);
  let session = null;
  return runRoleLifecycle({
    stagePrefix: role,
    /** 页面主流程会在每个页面和截图边界复扫全部已知凭据。 */
    async executeAction() {
      session = await runAcceptanceStage(`${role}.login`, () => login(
        page,
        runtime,
        role,
        "routing-policies",
        (created) => {
          session = created;
          secretRegistry.add(created.token);
        },
      ));
      /** 当前角色扫描器只返回脱敏计数，不允许页面快照进入结果。 */
      async function scan(label) {
        return scanBrowserCheckpoint(page, guards, secretRegistry, {
          activeToken: session.token,
          label: `${role}:${label}`,
        });
      }
      const pages = {};
      const screenshots = [];
      pages.routingPolicy = await runAcceptanceStage(
        `${role}.routing_policy.verify`,
        () => verifyRoutingPolicyPage(page, runtime, role, scan),
      );
      for (const viewport of VIEWPORTS) {
        await runAcceptanceStage(`${role}.routing_policy.capture.${viewport.name}`, async () => {
          await page.setViewportSize({ width: viewport.width, height: viewport.height });
          await assertLayout(page, viewport, ".routing-policies-panel");
          screenshots.push(await capture(page, role, "page-068", viewport, scan));
        });
      }
      pages.qualityScores = await runAcceptanceStage(
        `${role}.quality_score.verify`,
        () => verifyQualityScorePage(page, runtime, role, scan),
      );
      for (const viewport of VIEWPORTS) {
        await runAcceptanceStage(`${role}.quality_score.capture.${viewport.name}`, async () => {
          await page.setViewportSize({ width: viewport.width, height: viewport.height });
          await assertLayout(page, viewport, ".model-quality-scores-panel");
          screenshots.push(await capture(page, role, "page-069", viewport, scan));
        });
      }
      pages.requestLog = await runAcceptanceStage(
        `${role}.request_log.verify`,
        () => verifyRequestLogPage(page, runtime, scan, `${role}.request_log.verify`),
      );
      for (const viewport of VIEWPORTS) {
        await runAcceptanceStage(`${role}.request_log.capture.${viewport.name}`, async () => {
          await page.setViewportSize({ width: viewport.width, height: viewport.height });
          await assertLayout(page, viewport, ".request-logs-panel");
          screenshots.push(await capture(page, role, "request-log", viewport, scan));
        });
      }
      const writeBoundary = await runAcceptanceStage(
        `${role}.write_boundary.verify`,
        () => verifyWriteBoundary(page, runtime, role, session.token, scan, guards),
      );
      const lateResponse = await runAcceptanceStage(
        `${role}.late_response.verify`,
        () => verifyLateQualityResponse(page),
      );
      // 迟到响应释放并稳定后必须再次全表面扫描，不能沿用离页前的隐私结论。
      const privacy = await runAcceptanceStage(
        `${role}.privacy.final`,
        () => scan("late_response:after"),
      );
      const targetEvidence = await runAcceptanceStage(
        `${role}.target_evidence.verify`,
        () => assertRoleTargetEvidence(role, guards),
      );
      return {
        role,
        pages,
        writeBoundary,
        lateResponse,
        privacy,
        privacyCheckpointCount: guards.checkpointCount,
        screenshotCount: screenshots.length,
        screenshots,
        ...targetEvidence,
        externalRequestCount: 0,
        browserErrorCount: 0,
      };
    },
    /** 已创建 UI Session 时必须真实注销；缺少 Session 的失败登录不会伪造注销成功。 */
    async logoutAction() {
      if (!session) {
        return {
          completed: true,
          required: false,
          attemptCount: 0,
          responseStatuses: [],
          authStorageCleared: true,
        };
      }
      // 已创建 Session 必须走真实后端注销；数据库最终清理不能替代当前浏览器生命周期证明。
      return { required: true, ...await logoutStrict(page, guards, secretRegistry) };
    },
    /** Context 始终关闭，避免失败角色把 Storage 或活动页面带到下一角色。 */
    async closeAction() {
      await context.close();
    },
  });
}

/** viewer 直达四个受限 hash 均须回退 overview，且目标 API 请求和响应都必须为零。 */
async function runViewerRole(browser, runtime, secretRegistry) {
  const role = "viewer";
  const context = await browser.newContext({ viewport: VIEWPORTS[0] });
  const page = await context.newPage();
  const guards = await installBrowserGuards(context, page);
  let session = null;
  return runRoleLifecycle({
    stagePrefix: role,
    /** viewer 逐页验证前后隐私边界，确认受限 hash 不生成 DOM 也不触发预取。 */
    async executeAction() {
      session = await runAcceptanceStage(`${role}.login`, () => login(
        page,
        runtime,
        role,
        TARGET_NAVIGATIONS[0],
        (created) => {
          session = created;
          secretRegistry.add(created.token);
        },
      ));
      /** viewer 扫描器与读角色使用同一联合门禁，避免只检查最终 overview。 */
      async function scan(label) {
        return scanBrowserCheckpoint(page, guards, secretRegistry, {
          activeToken: session.token,
          label: `${role}:${label}`,
        });
      }
      const fallbacks = [];
      const screenshots = [];
      for (const navigation of TARGET_NAVIGATIONS) {
        await runAcceptanceStage(`${role}.navigation.${navigation}`, async () => {
          await page.evaluate((hash) => { window.location.hash = hash; }, navigation);
          await page.locator('.console-shell[data-active-navigation="overview"]').waitFor({
            state: "visible",
            timeout: 30_000,
          });
          await page.waitForTimeout(250);
          await scan(`${navigation}:page_before`);
          assert.equal(await page.locator(`[data-console-nav="${navigation}"]`).count(), 0);
          assert.equal(await page.locator(`.${navigation}-panel`).count(), 0);
          await scan(`${navigation}:page_after`);
          fallbacks.push({ requested: navigation, active: "overview", restrictedDom: false });
        });
      }
      for (const viewport of VIEWPORTS) {
        await runAcceptanceStage(`${role}.overview.capture.${viewport.name}`, async () => {
          await page.setViewportSize({ width: viewport.width, height: viewport.height });
          await assertLayout(page, viewport, "main");
          screenshots.push(await capture(page, role, "overview-fallback", viewport, scan));
        });
      }
      const targetEvidence = await runAcceptanceStage(
        `${role}.target_evidence.verify`,
        () => assertRoleTargetEvidence(role, guards),
      );
      const privacy = await runAcceptanceStage(
        `${role}.privacy.final`,
        () => scan("viewer:final"),
      );
      return {
        role,
        fallbacks,
        privacy,
        privacyCheckpointCount: guards.checkpointCount,
        screenshotCount: screenshots.length,
        screenshots,
        ...targetEvidence,
        externalRequestCount: 0,
        browserErrorCount: 0,
      };
    },
    /** viewer 也必须真实注销并核对 Storage，零业务 API 不代表可以遗留 Session。 */
    async logoutAction() {
      if (!session) {
        return {
          completed: true,
          required: false,
          attemptCount: 0,
          responseStatuses: [],
          authStorageCleared: true,
        };
      }
      // viewer 同样持有真实 Session，零目标 API 访问不构成省略注销的理由。
      return { required: true, ...await logoutStrict(page, guards, secretRegistry) };
    },
    /** viewer 独立 Context 在注销后关闭，不能与其他角色复用认证存储。 */
    async closeAction() {
      await context.close();
    },
  });
}

/** 在写结果前做最后一次内存级脱敏断言，避免新增字段误带凭据或正文哨兵。 */
function assertResultRedacted(result, exactSecrets = []) {
  assert.ok(!containsSecret(JSON.stringify(result), exactSecrets), "浏览器结果出现敏感信息");
  return true;
}

/** 启动真实 Chrome，顺序完成三角色验收，并仅写入通过脱敏门禁的结果。 */
async function main() {
  assertLocalScope();
  const runtime = await loadRuntimeState();
  const secretRegistry = collectConfiguredSecrets(runtime);
  await mkdir(ARTIFACT_DIR, { recursive: true });
  await rm(RESULT_PATH, { force: true });
  const { chromium } = require("playwright");
  const browser = await chromium.launch({
    executablePath: CHROME_PATH,
    headless: process.env.METAHUB_BROWSER_HEADED !== "1",
  });
  let result;
  try {
    const admin = await runReaderRole(browser, runtime, "admin", secretRegistry);
    const developer = await runReaderRole(browser, runtime, "developer", secretRegistry);
    const viewer = await runViewerRole(browser, runtime, secretRegistry);
    result = {
      status: "passed",
      prdId: "PRD-048",
      runtimeSuffix: runtime.suffix,
      admin,
      developer,
      viewer,
      viewportCount: VIEWPORTS.length,
      screenshotCount: admin.screenshotCount + developer.screenshotCount + viewer.screenshotCount,
    };
    assertResultRedacted(result, secretRegistry);
  } finally {
    await browser.close();
  }
  const encoded = `${JSON.stringify(result, null, 2)}\n`;
  await writeFile(RESULT_PATH, encoded, "utf8");
  process.stdout.write(encoded);
  // 所有角色结束后清空内存注册表，避免长生命周期 Node 进程继续持有测试凭据。
  secretRegistry.clear();
}

// 单元测试 import 时只暴露纯 helper；命令行直接执行才启动真实浏览器和文件写入。
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}

export {
  BrowserAcceptanceStageError,
  assertExpectedProbeConsoleCount,
  assertNetworkEvidencePrivacy,
  assertRequestLogQualityEvidenceCount,
  assertResultRedacted,
  assertRoleTargetEvidence,
  assertSensitiveSurfaceSnapshot,
  classifyTargetApi,
  collectConfiguredSecrets,
  containsSecret,
  decideBrowserNetworkRequest,
  executeLogoutWithRetry,
  installBrowserGuards,
  isExpectedProbeConsoleError,
  loadRuntimeState,
  runAcceptanceStage,
  runRoleLifecycle,
  toSafeStageError,
  validateRuntimeStateDocument,
  validateServiceRoot,
};
