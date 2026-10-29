/**
 * PRD-064 PAGE-091 Provider Adapter 契约报告真实浏览器验收器。
 *
 * 安全边界：脚本只访问固定 localhost 服务。浏览器 profile、TEMP、TMP、下载、截图与脱敏
 * JSON 证据只能写入 D:/MetaHub/.artifacts/prd064-browser/<run-id> 的全新普通目录。
 * 管理员口令、Session 凭据、租户标识、opaque cursor 和 API 正文只在内存中短暂使用，不写终端或证据。
 */

import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { lstat, mkdir, realpath, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";

/** CommonJS 桥只在 CLI 真正执行时加载桌面运行时 Playwright，不增加 Console 生产依赖。 */
const require = createRequire(import.meta.url);
/** 所有目录从脚本位置反算，调用命令的当前目录不能改变证据归属。 */
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
/** PRD-064 浏览器证据唯一允许的父目录。 */
const ARTIFACT_ROOT = path.join(REPOSITORY_ROOT, ".artifacts", "prd064-browser");
/** 登录期间只允许 Console 已有的两个 Session Storage 槽位。 */
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);
/** Excel 冻结的桌面和移动视口。 */
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
/** API 和报告枚举必须与 PAGE-091 严格客户端合同一致。 */
const RUN_STATUSES = new Set(["passed", "failed"]);
const SUITE_KINDS = new Set(["merge_gate", "probe"]);
const RUN_MODES = new Set(["mock", "gateway_probe"]);
const CASE_KINDS = new Set(["request", "non_stream", "stream", "error", "usage"]);
const AUTH_TYPES = new Set(["none", "bearer"]);
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
/** 页面可接受的 Adapter family 必须来自运行框架冻结集合。 */
const ADAPTER_NAMES = new Set(["openai_compatible", "generic_http"]);
/** 页面只允许展示以下脱敏失败分类，底层异常和断言正文不能进入 Control DTO。 */
const FAILURE_CODES = new Set([
  "adapter.contract_fixture_missing",
  "adapter.contract_fixture_unexpected",
  "adapter.contract_fixture_invalid",
  "adapter.contract_coverage_mismatch",
  "adapter.contract_request_failed",
  "adapter.contract_non_stream_failed",
  "adapter.contract_stream_failed",
  "adapter.contract_error_mapping_failed",
  "adapter.contract_usage_failed",
  "adapter.contract_probe_failed",
]);
/** 有界 ID 与 hash 语法用于拒绝控制字符、路径注入和自由格式报告身份。 */
const SAFE_ID = /^[A-Za-z0-9_.:-]{1,256}$/;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{0,159}$/;
const SAFE_VERSION = /^[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}$/;
const HASH = /^[a-f0-9]{64}$/;
const RUN_ID = /^acr_[a-f0-9]{32}$/;
const CASE_ID = /^acc_[a-f0-9]{32}$/;
const CURSOR = /^[A-Za-z0-9_-]+={0,2}$/;
/** 证据键名本身也失败关闭，避免以后把敏感值塞进看似已脱敏的槽位。 */
const FORBIDDEN_EVIDENCE_KEY = /password|token|secret|prompt|response|authorization|cookie|provider[_-]?key|base[_-]?url|endpoint|cursor|tenant|filter/i;
/** 浏览器历史与代理日志不允许出现这些查询键；分页 cursor 只存在 Control 请求，不进入页面 URL 或证据。 */
const FORBIDDEN_QUERY_KEY = /password|token|secret|prompt|response|authorization|cookie|provider[_-]?key|tenant[_-]?id|base[_-]?url|endpoint/i;
/** Control DTO 可携带 owner 字段做租户等值验证，但不能夹带任何调用正文、Key 或 Endpoint。 */
const FORBIDDEN_PAYLOAD_KEY = /password|token|secret|prompt|response|authorization|cookie|provider[_-]?key|base[_-]?url|endpoint|request[_-]?body|report[_-]?body/i;


/** 使用固定 stage/code 表达失败，避免 Playwright 原错、URL 或 DOM 正文进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存源码内固定失败分类，不复制任何外部输入。 */
  constructor(stage, code) {
    super(stage + ":" + code);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 失败关闭断言；stage/code 只能由调用方传入源码内固定常量。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** 把浏览器 API 原始异常折叠为固定分类，防止异常携带口令、URL 或页面正文。 */
async function runBrowserOperation(stage, operation) {
  try {
    return await operation();
  } catch (error) {
    // 已归类失败保留原 stage/code；未知浏览器异常才折叠，避免丢失稳定验收原因。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "operation_failed");
  }
}


/** 只接受固定端口 localhost HTTP Origin，拒绝别名、路径、查询、Hash 和 URL 凭据。 */
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
 * 只允许固定证据根下一级 run-id，拒绝父目录本身、嵌套目录、绝对逃逸和路径穿越。
 */
function resolveArtifactDirectory(value, repositoryRoot = REPOSITORY_ROOT) {
  const root = path.resolve(repositoryRoot, ".artifacts", "prd064-browser");
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


/** 校验目录不是链接节点，且没有经 Windows reparse point 解析到其他物理位置。 */
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


/** 独占创建全新批次和固定子目录；任何既有批次或链接都失败，绝不覆盖历史证据。 */
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
    // 目录安全错误保留精确分类，其余宿主文件系统细节统一隐藏。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure("config.artifact_directory", "create_failed");
  }
  return target;
}


/** 读取固定服务、管理员身份、Chrome 与证据目录；敏感值仅保留在当前 Node 进程内存。 */
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
  const controlBackendOrigin = environment.METAHUB_ACCEPTANCE_CONTROL_BACKEND_URL
    ? normalizeLoopbackOrigin(
        environment.METAHUB_ACCEPTANCE_CONTROL_BACKEND_URL,
        18001,
        "config.control_backend",
      )
    : controlOrigin;
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
  const artifactDirectory = await createArtifactDirectory(
    environment.METAHUB_ACCEPTANCE_ARTIFACT_DIR,
  );
  return Object.freeze({
    artifactDirectory,
    consoleOrigin,
    controlOrigin,
    controlBackendOrigin,
    gatewayOrigin,
    chromePath,
    tenantSlug,
    adminEmail,
    adminPassword,
    sensitiveCanary,
  });
}


/** 浏览器只继承 Windows 启动 allowlist，并把全部用户与临时目录绑定到当前证据批次。 */
function buildBrowserEnvironment(environment, artifactDirectory) {
  const sanitized = {};
  const requiredName = /^(?:path|pathext|systemroot|windir|comspec|os|number_of_processors|processor_(?:architecture|identifier)|lang|lc_[a-z_]+)$/i;
  for (const [key, value] of Object.entries(environment)) {
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
 * Playwright 临时 profile 由 Node 父进程创建，只设置 Chromium env 仍可能回退宿主临时目录。
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


/** 只放行三个固定本机 Origin 和浏览器内部 URL，并拒绝 URL 凭据与敏感查询键。 */
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


/** 判断 URL 是否精确只包含允许的单值参数，拒绝重复键和未知参数。 */
function hasOnlySingleSearchParams(target, allowedKeys) {
  const entries = [...target.searchParams.entries()];
  if (entries.some(([key]) => !allowedKeys.has(key))) return false;
  return [...new Set(entries.map(([key]) => key))].every(
    (key) => target.searchParams.getAll(key).length === 1,
  );
}


/**
 * 识别 PAGE-091 列表或详情 GET 合同。
 * 返回值不保留 Bearer、cursor 原值或 URL；null 表示联网前合同失败。
 */
function adapterContractRequestDescriptor(request, controlOrigin) {
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
    || request.method() !== "GET"
    || !/^Bearer\s+\S{8,}$/.test(authorization)
  ) return null;
  if (target.pathname === "/adapter-contract-runs") {
    const allowed = new Set(["status", "suite_kind", "mode", "limit", "cursor"]);
    const suiteKind = target.searchParams.get("suite_kind") ?? "";
    const status = target.searchParams.get("status") ?? "";
    const mode = target.searchParams.get("mode") ?? "";
    const cursor = target.searchParams.get("cursor");
    if (
      !hasOnlySingleSearchParams(target, allowed)
      || !SUITE_KINDS.has(suiteKind)
      || (status && !RUN_STATUSES.has(status))
      || (mode && !RUN_MODES.has(mode))
      || target.searchParams.get("limit") !== "50"
      || (cursor !== null && (!cursor || cursor.length > 4096 || !CURSOR.test(cursor)))
      || !/^console_adapter_contract_list_\d+_[A-Za-z0-9]{6,20}$/.test(requestId)
    ) return null;
    return Object.freeze({
      kind: "list",
      suite_kind: suiteKind,
      status,
      mode,
      cursor_present: cursor !== null,
    });
  }
  const detailMatch = target.pathname.match(/^\/adapter-contract-runs\/(acr_[a-f0-9]{32})$/);
  if (
    detailMatch
    && !target.search
    && /^console_adapter_contract_detail_\d+_[A-Za-z0-9]{6,20}$/.test(requestId)
  ) {
    return Object.freeze({ kind: "detail", run_id: detailMatch[1] });
  }
  return null;
}


/**
 * 为专用 UI 分页步骤构造真实 Control 后端 URL。
 * 只允许已冻结的列表参数，把 limit 从生产默认 50 缩为 1；Origin、suite/status/mode/cursor 原样保留。
 */
function buildPaginationBackendUrl(value, controlOrigin) {
  let target;
  try {
    target = new URL(String(value));
  } catch {
    throw new BrowserAcceptanceFailure("browser.pagination_proxy", "url_invalid");
  }
  const allowed = new Set(["status", "suite_kind", "mode", "limit", "cursor"]);
  const suiteKind = target.searchParams.get("suite_kind") ?? "";
  const status = target.searchParams.get("status") ?? "";
  const mode = target.searchParams.get("mode") ?? "";
  const cursor = target.searchParams.get("cursor");
  requireCondition(
    target.origin === controlOrigin
      && target.pathname === "/adapter-contract-runs"
      && !target.hash
      && !target.username
      && !target.password
      && hasOnlySingleSearchParams(target, allowed)
      && SUITE_KINDS.has(suiteKind)
      && (!status || RUN_STATUSES.has(status))
      && (!mode || RUN_MODES.has(mode))
      && target.searchParams.get("limit") === "50"
      && (cursor === null || (cursor.length <= 4096 && CURSOR.test(cursor))),
    "browser.pagination_proxy",
    "request_contract",
  );
  target.searchParams.set("limit", "1");
  return target.toString();
}


/**
 * 把浏览器固定请求的 8001 Origin 映射到隔离 Control Origin，只替换协议、主机和端口。
 * 路径、查询、Header 与正文继续由 Playwright ``route.fetch`` 原样转发，避免验收器重建业务请求。
 */
function mapControlBackendUrl(value, controlOrigin, controlBackendOrigin) {
  let target;
  let backend;
  try {
    target = new URL(String(value));
    backend = new URL(String(controlBackendOrigin));
  } catch {
    throw new BrowserAcceptanceFailure("browser.control_proxy", "url_invalid");
  }
  requireCondition(
    target.origin === controlOrigin
      && !target.username
      && !target.password
      && backend.origin === controlBackendOrigin
      && backend.pathname === "/"
      && !backend.search
      && !backend.hash
      && !backend.username
      && !backend.password,
    "browser.control_proxy",
    "origin_contract",
  );
  target.protocol = backend.protocol;
  target.hostname = backend.hostname;
  target.port = backend.port;
  return target.toString();
}


/** 深度扫描 Control DTO 键名，防止调用正文、Key、Token 或 Endpoint 混入只读报告。 */
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


/** 验证无首尾空白、无控制字符且符合模式的有界字符串。 */
function strictText(value, pattern, maximumLength = 256) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= maximumLength
    && value === value.trim()
    && !/[\u0000-\u001f\u007f]/.test(value)
    && pattern.test(value)
    ? value
    : "";
}


/** 验证 0..2048 的安全整数，拒绝字符串隐式转换。 */
function boundedCount(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= 2048 ? value : null;
}


/** 返回可比较的有效时间戳；原字符串只在当前响应校验期间存在。 */
function validTime(value) {
  if (typeof value !== "string" || value.length > 64 || value !== value.trim()) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}


/** 验证 failure code 集合唯一、稳定排序且全部属于冻结分类。 */
function validFailureCodes(value) {
  if (!Array.isArray(value) || value.length > FAILURE_CODES.size) return false;
  return value.every((item) => strictText(item, SAFE_CODE) && FAILURE_CODES.has(item))
    && new Set(value).size === value.length
    && JSON.stringify(value) === JSON.stringify([...value].sort());
}


/** 验证并压缩一条运行事实；返回对象只供本次内存内的列表/详情交叉校验。 */
function validateRun(value) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), "browser.run_payload", "object");
  const runId = strictText(value.adapter_contract_run_id, RUN_ID);
  const ownerId = strictText(value.tenant_id, SAFE_ID);
  const suiteKind = strictText(value.suite_kind, SAFE_CODE);
  const mode = strictText(value.mode, SAFE_CODE);
  const status = strictText(value.status, SAFE_CODE);
  const total = boundedCount(value.total_cases);
  const passed = boundedCount(value.passed_cases);
  const failed = boundedCount(value.failed_cases);
  const startedAt = validTime(value.started_at);
  const completedAt = validTime(value.completed_at);
  const createdAt = validTime(value.created_at);
  requireCondition(Boolean(
    runId
      && ownerId
      && strictText(value.source_request_id, SAFE_ID)
      && strictText(value.source_trace_id, SAFE_ID)
      && value.schema_version === "adapter-contract-report-v1"
      && strictText(value.catalog_version, SAFE_VERSION, 80)
      && strictText(value.catalog_hash, HASH, 64)
      && strictText(value.runner_version, SAFE_VERSION, 80)
      && strictText(value.source_revision_hash, HASH, 64)
      && strictText(value.report_hash, HASH, 64)
      && SUITE_KINDS.has(suiteKind)
      && RUN_MODES.has(mode)
      && RUN_STATUSES.has(status)
      && validFailureCodes(value.failure_codes)
      && startedAt !== null
      && completedAt !== null
      && createdAt !== null
      && completedAt >= startedAt
      && createdAt >= completedAt
      && total !== null
      && passed !== null
      && failed !== null
      && total === passed + failed
      && typeof value.coverage_complete === "boolean"
      && (suiteKind !== "merge_gate" || (mode === "mock" && value.coverage_complete === true))
      && (suiteKind !== "probe" || (mode === "gateway_probe" && value.coverage_complete === false))
      && (status !== "passed" || (failed === 0 && value.failure_codes.length === 0))
      && (status !== "failed" || (failed > 0 && value.failure_codes.length > 0))
  ), "browser.run_payload", "contract");
  return Object.freeze({
    run_id: runId,
    owner_id: ownerId,
    suite_kind: suiteKind,
    mode,
    status,
    total,
    passed,
    failed,
    completed_at: completedAt,
  });
}


/** 验证列表响应的租户 owner、筛选语义、分页、排序、唯一性和 no-body 隐私边界。 */
function validateAdapterContractListPayload(payload, expectedOwnerId, descriptor, expectedLimit = 50) {
  requireCondition(
    payload && typeof payload === "object" && !Array.isArray(payload),
    "browser.list_payload",
    "object",
  );
  requireCondition(!hasForbiddenPayloadKey(payload), "browser.list_payload", "sensitive_key");
  requireCondition(descriptor?.kind === "list", "browser.list_payload", "request_contract");
  requireCondition(
    Number.isSafeInteger(expectedLimit) && expectedLimit >= 1 && expectedLimit <= 100,
    "browser.list_payload",
    "expected_limit",
  );
  requireCondition(Array.isArray(payload.adapter_contract_runs), "browser.list_payload", "runs");
  requireCondition(payload.adapter_contract_runs.length <= expectedLimit, "browser.list_payload", "limit");
  requireCondition(payload.limit === expectedLimit, "browser.list_payload", "limit_echo");
  requireCondition(
    payload.next_cursor === null
      || (typeof payload.next_cursor === "string"
        && payload.next_cursor.length <= 4096
        && CURSOR.test(payload.next_cursor)),
    "browser.list_payload",
    "next_page",
  );
  requireCondition(strictText(payload.request_id, SAFE_ID), "browser.list_payload", "request_id");
  requireCondition(strictText(payload.trace_id, SAFE_ID), "browser.list_payload", "trace_id");
  const runs = payload.adapter_contract_runs.map(validateRun);
  requireCondition(
    runs.every((run) => run.owner_id === expectedOwnerId),
    "browser.list_payload",
    "owner_mismatch",
  );
  requireCondition(
    new Set(runs.map((run) => run.run_id)).size === runs.length,
    "browser.list_payload",
    "duplicate_run",
  );
  requireCondition(
    runs.every((run) => (
      run.suite_kind === descriptor.suite_kind
      && (!descriptor.status || run.status === descriptor.status)
      && (!descriptor.mode || run.mode === descriptor.mode)
    )),
    "browser.list_payload",
    "query_mismatch",
  );
  requireCondition(
    runs.every((run, index) => index === 0 || runs[index - 1].completed_at >= run.completed_at),
    "browser.list_payload",
    "order",
  );
  return Object.freeze({
    entry_count: runs.length,
    has_next_page: payload.next_cursor !== null,
    first_status: runs[0]?.status ?? null,
    first_run_id: runs[0]?.run_id ?? null,
    suite_kind: descriptor.suite_kind,
    mode: descriptor.mode,
  });
}


/** 验证一条 case 的复合 owner、枚举、状态/失败码配对与有界耗时。 */
function validateCase(value, expectedOwnerId, expectedRunId) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), "browser.case_payload", "object");
  const status = strictText(value.status, SAFE_CODE);
  const failureCode = value.failure_code === null ? null : strictText(value.failure_code, SAFE_CODE);
  requireCondition(Boolean(
    strictText(value.adapter_contract_case_id, CASE_ID)
      && value.tenant_id === expectedOwnerId
      && value.adapter_contract_run_id === expectedRunId
      && strictText(value.contract_case_key, SAFE_ID)
      && strictText(value.spec_id, SAFE_ID, 160)
      && PROVIDER_TYPES.has(strictText(value.provider_type, SAFE_CODE))
      && ADAPTER_NAMES.has(strictText(value.adapter_name, SAFE_CODE))
      && strictText(value.adapter_version, SAFE_VERSION, 80)
      && CASE_KINDS.has(strictText(value.case_kind, SAFE_CODE))
      && strictText(value.variant, SAFE_CODE)
      && (value.auth_type === null || AUTH_TYPES.has(strictText(value.auth_type, SAFE_CODE)))
      && RUN_STATUSES.has(status)
      && (status !== "passed" || failureCode === null)
      && (status !== "failed" || FAILURE_CODES.has(failureCode))
      && Number.isSafeInteger(value.duration_ms)
      && value.duration_ms >= 0
      && value.duration_ms <= 300000
      && strictText(value.evidence_hash, HASH, 64)
      && validTime(value.created_at) !== null
  ), "browser.case_payload", "contract");
  return Object.freeze({
    case_id: value.adapter_contract_case_id,
    case_key: value.contract_case_key,
    provider_type: value.provider_type,
    status,
  });
}


/** 验证详情父 run、复合租户 owner、case 数量/唯一性和通过失败计数。 */
function validateAdapterContractDetailPayload(payload, expectedOwnerId, expectedRunId) {
  requireCondition(
    payload && typeof payload === "object" && !Array.isArray(payload),
    "browser.detail_payload",
    "object",
  );
  requireCondition(!hasForbiddenPayloadKey(payload), "browser.detail_payload", "sensitive_key");
  requireCondition(strictText(payload.request_id, SAFE_ID), "browser.detail_payload", "request_id");
  requireCondition(strictText(payload.trace_id, SAFE_ID), "browser.detail_payload", "trace_id");
  const run = validateRun(payload.adapter_contract_run);
  requireCondition(
    run.run_id === expectedRunId && run.owner_id === expectedOwnerId,
    "browser.detail_payload",
    "owner_mismatch",
  );
  requireCondition(Array.isArray(payload.cases) && payload.cases.length <= 2048, "browser.detail_payload", "cases");
  const cases = payload.cases.map((item) => validateCase(item, expectedOwnerId, expectedRunId));
  requireCondition(cases.length === run.total, "browser.detail_payload", "count");
  requireCondition(new Set(cases.map((item) => item.case_id)).size === cases.length, "browser.detail_payload", "duplicate_case");
  requireCondition(new Set(cases.map((item) => item.case_key)).size === cases.length, "browser.detail_payload", "duplicate_key");
  requireCondition(
    cases.filter((item) => item.status === "passed").length === run.passed
      && cases.filter((item) => item.status === "failed").length === run.failed,
    "browser.detail_payload",
    "status_count",
  );
  return Object.freeze({
    case_count: cases.length,
    provider_count: new Set(cases.map((item) => item.provider_type)).size,
    failed_count: run.failed,
  });
}


/** 验证用于浏览器故障投影的统一 403/503 响应和 no-store 链路字段。 */
function validateUnifiedErrorPayload(payload, headers, expectedStatus) {
  const expectedCode = expectedStatus === 403
    ? "auth.permission_denied"
    : "adapter.contract_storage_unavailable";
  requireCondition([403, 503].includes(expectedStatus), "browser.error_payload", "status");
  requireCondition(
    payload
      && typeof payload === "object"
      && !Array.isArray(payload)
      && payload.code === expectedCode
      && typeof payload.message === "string"
      && payload.message.length > 0
      && payload.message.length <= 500
      && strictText(payload.request_id, SAFE_ID)
      && strictText(payload.trace_id, SAFE_ID)
      && String(headers?.get?.("Cache-Control") ?? "").toLowerCase().includes("no-store")
      && headers?.get?.("X-Request-ID") === payload.request_id
      && headers?.get?.("X-Trace-ID") === payload.trace_id
      && !hasForbiddenPayloadKey(payload),
    "browser.error_payload",
    "contract",
  );
  return Object.freeze({ status: expectedStatus, code_confirmed: true, no_store_confirmed: true });
}


/** 验证后端导航确实签发 PAGE-091 owner；前端按钮存在不能替代这个检查。 */
function validateNavigationOwner(payload) {
  const items = payload?.navigation_items;
  requireCondition(Array.isArray(items), "browser.navigation", "items");
  const matches = items.filter((item) => (
    item?.id === "adapter-contract-runs"
    && item?.target_section === "adapter-contract-runs"
    && item?.visible !== false
    && item?.disabled !== true
    && Array.isArray(item?.required_permissions)
    && item.required_permissions.includes("provider.health.read")
  ));
  requireCondition(matches.length === 1, "browser.navigation", "owner");
  return true;
}


/** 仅允许确定性故障注入期间的浏览器资源 403/503；其余 console error 一律失败。 */
function classifyBrowserConsoleMessage({ type, text = "" } = {}, expectedFaultStatuses = []) {
  if (type !== "error") return "ignored";
  const expected = expectedFaultStatuses.some(
    (status) => [403, 503].includes(status) && new RegExp("\\b" + status + "\\b").test(String(text)),
  );
  return expected && /Failed to load resource/i.test(String(text))
    ? "expected_fault"
    : "unexpected_error";
}


/** 检查 Console 文本是否携带凭据、Bearer 或敏感字段名；只返回布尔值，不保留原文。 */
function browserConsolePrivacyViolation(text, secrets = []) {
  const value = String(text ?? "");
  return /Bearer\s+\S+|authorization|provider[_ -]?key|prompt|endpoint|secret|session[_ -]?token/i.test(value)
    || secrets.filter(Boolean).some((secret) => value.includes(secret));
}


/** 构造只用于下一次列表 GET 的固定统一错误，禁止调用方注入消息或链路值。 */
function deterministicFaultPayload(status) {
  return status === 403
    ? {
        code: "auth.permission_denied",
        message: "当前用户无权查看契约测试报告",
        request_id: "req_prd064_browser_fault_403",
        trace_id: "trc_prd064_browser_fault_403",
      }
    : {
        code: "adapter.contract_storage_unavailable",
        message: "契约测试运行暂不可用",
        request_id: "req_prd064_browser_fault_503",
        trace_id: "trc_prd064_browser_fault_503",
      };
}


/** 扫描单个请求的 URL、Header 与正文；登录口令只允许出现在真实登录 POST 正文。 */
function requestPrivacyViolation(request, target, config) {
  const headers = request.headers?.() ?? {};
  const body = String(request.postData?.() ?? "");
  const authorization = String(headers.authorization ?? headers.Authorization ?? "");
  const sensitiveHeader = Object.keys(headers).some(
    (key) => /provider[_-]?key|api[_-]?key|secret|prompt|response|endpoint/i.test(key),
  );
  const wrongAuthorizationDestination = Boolean(authorization) && target.origin !== config.controlOrigin;
  const values = [config.adminPassword, config.sensitiveCanary].filter(Boolean);
  const urlLeak = values.some((value) => request.url().includes(value));
  const bodyLeak = target.pathname === "/auth/login"
    ? values.slice(1).some((value) => body.includes(value))
    : values.some((value) => body.includes(value));
  const bodyShapeLeak = target.pathname !== "/auth/login"
    && /provider[_-]?key|api[_-]?key|authorization|prompt|response|endpoint|secret/i.test(body);
  // 任一边界命中都拒绝请求；调用方只累计次数，不保存触发内容。
  return sensitiveHeader || wrongAuthorizationDestination || urlLeak || bodyLeak || bodyShapeLeak;
}


/**
 * 安装固定 Origin 路由门禁、网络/Console 隐私计数和一次性 403/503 故障注入。
 * tracker 只保存整数和布尔事实，绝不保存 URL、Header、正文、cursor 或身份值。
 */
async function installBrowserGuards(context, page, config) {
  const allowedOrigins = new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]);
  let pendingFaultStatus = null;
  let paginationProxyEnabled = false;
  const expectedFaultConsoleBudgets = new Map([[403, 0], [503, 0]]);
  const tracker = {
    blockedRequestCount: 0,
    invalidContractRequestCount: 0,
    networkPrivacyViolationCount: 0,
    consolePrivacyViolationCount: 0,
    pageErrorCount: 0,
    consoleErrorCount: 0,
    expectedFaultConsoleCount: 0,
    listRequestCount: 0,
    detailRequestCount: 0,
    paginationProxyRequestCount: 0,
    paginationProxyFailureCount: 0,
    controlBackendProxyRequestCount: 0,
    controlBackendProxyFailureCount: 0,
    faultCounts: { 403: 0, 503: 0 },
    loginStatuses: [],
    meStatuses: [],
    navigationStatuses: [],
    listStatuses: [],
    detailStatuses: [],
    logoutStatuses: [],
    /** 只允许 403 或 503，且上一故障必须已经被列表 GET 消费。 */
    armFault(status) {
      requireCondition([403, 503].includes(status), "browser.fault", "status");
      requireCondition(pendingFaultStatus === null, "browser.fault", "already_pending");
      pendingFaultStatus = status;
    },
    /** 返回故障是否被真实页面请求消费，不暴露任何请求信息。 */
    faultConsumed(status) {
      return tracker.faultCounts[status] === 1 && pendingFaultStatus === null;
    },
    /** 页面已经接纳错误响应后关闭短暂 Console 豁免窗口，后续同码错误仍必须失败。 */
    closeFaultConsoleWindow(status) {
      expectedFaultConsoleBudgets.set(status, 0);
    },
    /** 专用分页步骤把同源 Control 请求缩为 limit=1，响应仍来自真实后端 cursor 查询。 */
    enablePaginationProxy() {
      requireCondition(!paginationProxyEnabled, "browser.pagination_proxy", "already_enabled");
      paginationProxyEnabled = true;
    },
    /** 分页往返结束后立即关闭代理，后续筛选和故障继续验证生产默认 limit=50。 */
    disablePaginationProxy() {
      paginationProxyEnabled = false;
    },
  };
  page.on("pageerror", () => { tracker.pageErrorCount += 1; });
  page.on("console", (message) => {
    const text = message.text();
    if (browserConsolePrivacyViolation(text, [config.adminPassword, config.sensitiveCanary])) {
      tracker.consolePrivacyViolationCount += 1;
    }
    const classification = classifyBrowserConsoleMessage(
      { type: message.type(), text },
      [...expectedFaultConsoleBudgets.entries()]
        .filter(([, budget]) => budget > 0)
        .map(([status]) => status),
    );
    // 未预期 error 独立计数；普通 log/warning 不误伤实际验收。
    if (classification === "unexpected_error") tracker.consoleErrorCount += 1;
    if (classification === "expected_fault") {
      tracker.expectedFaultConsoleCount += 1;
      const matchedStatus = [403, 503].find(
        (status) => expectedFaultConsoleBudgets.get(status) > 0
          && new RegExp("\\b" + status + "\\b").test(text),
      );
      if (matchedStatus) expectedFaultConsoleBudgets.set(matchedStatus, 0);
    }
  });
  page.on("response", (response) => {
    try {
      const target = new URL(response.url());
      if (target.origin !== config.controlOrigin) return;
      const method = response.request().method();
      // 登录和身份状态分别证明认证建立与服务端 Session 校验成功。
      if (target.pathname === "/auth/login" && method === "POST") tracker.loginStatuses.push(response.status());
      if (target.pathname === "/auth/me" && method === "GET") tracker.meStatuses.push(response.status());
      if (target.pathname === "/console/navigation" && method === "GET") {
        tracker.navigationStatuses.push(response.status());
      }
      if (target.pathname === "/adapter-contract-runs" && method === "GET") {
        tracker.listStatuses.push(response.status());
      }
      if (/^\/adapter-contract-runs\/acr_[a-f0-9]{32}$/.test(target.pathname) && method === "GET") {
        tracker.detailStatuses.push(response.status());
      }
      // 注销状态单独证明服务端 Session 失效，不能只依据前端清空外观。
      if (target.pathname === "/auth/logout" && method === "POST") tracker.logoutStatuses.push(response.status());
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
    if (requestPrivacyViolation(request, target, config)) {
      tracker.networkPrivacyViolationCount += 1;
      await route.abort("blockedbyclient");
      return;
    }
    const contractPath = target.origin === config.controlOrigin
      && (target.pathname === "/adapter-contract-runs"
        || /^\/adapter-contract-runs\//.test(target.pathname));
    if (contractPath && request.method() === "GET") {
      const descriptor = adapterContractRequestDescriptor(request, config.controlOrigin);
      if (!descriptor) {
        tracker.invalidContractRequestCount += 1;
        await route.abort("blockedbyclient");
        return;
      }
      if (descriptor.kind === "list") tracker.listRequestCount += 1;
      if (descriptor.kind === "detail") tracker.detailRequestCount += 1;
      if (descriptor.kind === "list" && pendingFaultStatus !== null) {
        const status = pendingFaultStatus;
        pendingFaultStatus = null;
        tracker.faultCounts[status] += 1;
        expectedFaultConsoleBudgets.set(status, 1);
        const payload = deterministicFaultPayload(status);
        await route.fulfill({
          status,
          contentType: "application/json; charset=utf-8",
          headers: {
            "Access-Control-Allow-Origin": config.consoleOrigin,
            "Cache-Control": "no-store",
            "X-Request-ID": payload.request_id,
            "X-Trace-ID": payload.trace_id,
          },
          body: JSON.stringify(payload),
        });
        return;
      }
      if (descriptor.kind === "list" && paginationProxyEnabled) {
        // 只改已通过精确合同校验的同源 limit；Bearer、suite/status/mode/cursor 均由 route.fetch 原样转发。
        const paginationUrl = buildPaginationBackendUrl(request.url(), config.controlOrigin);
        const backendUrl = mapControlBackendUrl(
          paginationUrl,
          config.controlOrigin,
          config.controlBackendOrigin,
        );
        try {
          // 禁止跟随重定向，确保 Bearer 只发送给冻结 Control Origin。
          const backendResponse = await route.fetch({ url: backendUrl, maxRedirects: 0 });
          tracker.paginationProxyRequestCount += 1;
          await route.fulfill({ response: backendResponse });
        } catch {
          // 代理失败只保留计数并中止请求，Playwright 原错和 URL 不进入终端或证据。
          tracker.paginationProxyFailureCount += 1;
          await route.abort("failed");
        }
        return;
      }
    }
    if (target.origin === config.controlOrigin && config.controlBackendOrigin !== config.controlOrigin) {
      try {
        const backendUrl = mapControlBackendUrl(
          request.url(),
          config.controlOrigin,
          config.controlBackendOrigin,
        );
        const backendResponse = await route.fetch({ url: backendUrl, maxRedirects: 0 });
        tracker.controlBackendProxyRequestCount += 1;
        await route.fulfill({ response: backendResponse });
      } catch {
        // 代理异常只保留计数；后端 URL、请求 Header、正文和 Playwright 原错均不进入证据。
        tracker.controlBackendProxyFailureCount += 1;
        await route.abort("failed");
      }
      return;
    }
    await route.continue();
  });
  return tracker;
}


/** 判断请求描述是否与预期 suite/status/mode、分页或详情 owner 完全一致。 */
function descriptorMatches(actual, expected) {
  if (!actual || actual.kind !== expected.kind) return false;
  if (actual.kind === "detail") return actual.run_id === expected.run_id;
  return actual.suite_kind === expected.suite_kind
    && actual.status === (expected.status ?? "")
    && actual.mode === (expected.mode ?? "")
    && actual.cursor_present === Boolean(expected.cursor_present);
}


/** 等待 PAGE-091 指定 GET 与状态码，不匹配的相邻页面请求不会误结束等待。 */
function waitForContractResponse(page, config, expected, status = 200) {
  return page.waitForResponse((response) => (
    response.status() === status
    && descriptorMatches(
      adapterContractRequestDescriptor(response.request(), config.controlOrigin),
      expected,
    )
  ), { timeout: 30000 });
}


/** 读取并验证真实列表 200；响应正文只在内存内完成合同检查。 */
async function readListResponse(
  response,
  controlOrigin,
  expectedOwnerId,
  expected,
  expectedLimit = 50,
) {
  requireCondition(response.status() === 200, "browser.list", "status");
  requireCondition(
    String(response.headers()["cache-control"] ?? "").toLowerCase().includes("no-store"),
    "browser.list",
    "no_store",
  );
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  // 必须使用配置阶段冻结的 Control Origin，不能从待验证响应自身反推并信任其 Origin。
  const descriptor = adapterContractRequestDescriptor(response.request(), controlOrigin);
  requireCondition(descriptorMatches(descriptor, expected), "browser.list", "request");
  return validateAdapterContractListPayload(payload, expectedOwnerId, descriptor, expectedLimit);
}


/** 读取并验证真实详情 200；case 原始字段和 owner 不会返回到最终证据。 */
async function readDetailResponse(response, expectedOwnerId, expectedRunId) {
  requireCondition(response.status() === 200, "browser.detail", "status");
  requireCondition(
    String(response.headers()["cache-control"] ?? "").toLowerCase().includes("no-store"),
    "browser.detail",
    "no_store",
  );
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  return validateAdapterContractDetailPayload(payload, expectedOwnerId, expectedRunId);
}


/** 通过真实表单建立管理员 Session，并同时验证 /auth/me 与后端 PAGE-091 导航 owner。 */
async function loginAdministrator(page, config) {
  await page.goto(config.consoleOrigin + "/", { waitUntil: "domcontentloaded" });
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30000 });
  await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
  await form.locator('[name="email"]').fill(config.adminEmail);
  await form.locator('[name="password"]').fill(config.adminPassword);
  const responsesPromise = Promise.all([
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
    page.waitForResponse((candidate) => {
      try {
        const target = new URL(candidate.url());
        return target.origin === config.controlOrigin
          && target.pathname === "/auth/me"
          && candidate.request().method() === "GET";
      } catch {
        return false;
      }
    }, { timeout: 30000 }),
    page.waitForResponse((candidate) => {
      try {
        const target = new URL(candidate.url());
        return target.origin === config.controlOrigin
          && target.pathname === "/console/navigation"
          && candidate.request().method() === "GET";
      } catch {
        return false;
      }
    }, { timeout: 30000 }),
  ]);
  await form.locator('button[type="submit"]').click();
  const [loginResponse, identityResponse, navigationResponse] = await responsesPromise;
  requireCondition(loginResponse.status() === 200, "browser.login", "status");
  requireCondition(identityResponse.status() === 200, "browser.login", "identity_status");
  requireCondition(navigationResponse.status() === 200, "browser.login", "navigation_status");
  let navigationPayload;
  try {
    navigationPayload = await navigationResponse.json();
  } catch {
    navigationPayload = null;
  }
  validateNavigationOwner(navigationPayload);
  await Promise.all([
    page.locator("#logout-button").waitFor({ state: "visible", timeout: 30000 }),
    page.locator(".console-shell").waitFor({ state: "visible", timeout: 30000 }),
    page.locator('[data-console-nav="adapter-contract-runs"]').waitFor({ state: "visible", timeout: 30000 }),
  ]);
  const identity = await page.evaluate((authKeys) => {
    try {
      const context = JSON.parse(sessionStorage.getItem("metahub.auth_context") || "null");
      const ownerId = String(context?.user?.tenant_id ?? context?.tenant?.tenant_id ?? "");
      return {
        administrator: context?.user?.role === "admin",
        owner_id: ownerId,
        session_value_present: String(sessionStorage.getItem("metahub.auth_token") || "").length >= 8,
        auth_slot_count: authKeys.filter((key) => sessionStorage.getItem(key) !== null).length,
      };
    } catch {
      // 损坏认证快照失败关闭为无身份事实，不回传原 Storage 内容。
      return { administrator: false, owner_id: "", session_value_present: false, auth_slot_count: 0 };
    }
  }, AUTH_STORAGE_KEYS);
  requireCondition(identity.administrator, "browser.login", "administrator_required");
  requireCondition(strictText(identity.owner_id, SAFE_ID), "browser.login", "owner_required");
  requireCondition(identity.session_value_present, "browser.login", "session_required");
  requireCondition(identity.auth_slot_count === AUTH_STORAGE_KEYS.length, "browser.login", "auth_slots");
  return Object.freeze({
    administrator_confirmed: true,
    navigation_owner_confirmed: true,
    owner_id: identity.owner_id,
  });
}


/** 等待列表刷新替换旧 PAGE-091 根节点，防止响应头先到时读取旧筛选 DOM。 */
async function waitForReportRootReplacement(page, previousRoot) {
  if (previousRoot) {
    await page.waitForFunction((root) => !root.isConnected, previousRoot, { timeout: 30000 });
    await previousRoot.dispose();
  }
  const root = page.locator("[data-adapter-contract-root]").first();
  await root.waitFor({ state: "visible", timeout: 30000 });
  return root;
}


/** 执行一次页面动作、读取真实列表响应，并等待新 DOM owner 完成替换。 */
async function performListAction(page, config, ownerId, expected, action, expectedLimit = 50) {
  const currentRoot = page.locator("[data-adapter-contract-root]").first();
  const previousRoot = await currentRoot.count() ? await currentRoot.elementHandle() : null;
  const responsePromise = waitForContractResponse(page, config, expected, 200);
  await action();
  const response = await responsePromise;
  const summary = await readListResponse(
    response,
    config.controlOrigin,
    ownerId,
    expected,
    expectedLimit,
  );
  await waitForReportRootReplacement(page, previousRoot);
  return summary;
}


/**
 * 验证当前 PAGE-091 摘要、分段、筛选、运行表、空态和 DOM 隐私。
 * owner 与秘密仅用于等值排除，不会复制到返回摘要。
 */
async function assertReportDom(page, expected, ownerId, config, baselineSummary = null) {
  const root = page.locator("[data-adapter-contract-root]").first();
  await root.waitFor({ state: "visible", timeout: 30000 });
  if (expected.entry_count > 0) {
    await root.locator(".adapter-contract-run-row:not(.adapter-contract-run-head)").first()
      .waitFor({ state: "visible", timeout: 30000 });
  } else {
    await root.locator(".adapter-contract-empty").waitFor({ state: "visible", timeout: 30000 });
  }
  const facts = await root.evaluate((element, input) => {
    const rows = [...element.querySelectorAll(".adapter-contract-run-row:not(.adapter-contract-run-head)")];
    const summary = element.querySelector(".adapter-contract-summary");
    const activeSuite = element.querySelector('[data-adapter-contract-suite][aria-pressed="true"]');
    const statusValue = element.querySelector("#adapter-contract-status-filter")?.value ?? "";
    const modeValue = element.querySelector("#adapter-contract-mode-filter")?.value ?? "";
    const html = element.outerHTML;
    const text = element.textContent || "";
    const suppliedSecrets = [input.password, input.canary].filter(Boolean);
    return {
      heading_ok: text.includes("Adapter 契约测试报告") && text.includes("PAGE-091"),
      summary_item_count: summary?.children.length ?? 0,
      summary_available: Boolean(summary) && !summary.textContent.includes("暂无记录"),
      summary_signature: summary?.textContent.replace(/\s+/g, " ").trim() ?? "",
      active_suite: activeSuite?.getAttribute("data-adapter-contract-suite") ?? "",
      status_value: statusValue,
      mode_value: modeValue,
      run_row_count: rows.length,
      status_rows_ok: !input.status || rows.every((row) => row.classList.contains("status-" + input.status)),
      mode_rows_ok: !input.mode || rows.every((row) => (
        row.textContent.includes(input.mode === "mock" ? "离线 Mock" : "Gateway 探测")
      )),
      empty_visible: Boolean(element.querySelector(".adapter-contract-empty"))
        && text.includes("当前筛选条件下没有契约测试运行"),
      request_id_present: ![...element.querySelectorAll(".adapter-contract-request-id")]
        .some((node) => node.textContent.trim().endsWith(": -")),
      dangerous_control_count: element.querySelectorAll(
        '[name="tenant_id"],[name*="password" i],[name*="token" i],[name*="secret" i],[name*="endpoint" i],[data-provider-endpoint]',
      ).length,
      execution_command_count: [...element.querySelectorAll("button,a")].filter(
        (node) => /运行探测|立即探测|重新运行|重跑/.test(node.textContent || ""),
      ).length,
      script_count: element.querySelectorAll("script").length,
      callable_url_count: (text.match(/https?:\/\//gi) ?? []).length,
      raw_field_name_count: (
        html.match(/tenant_id|source_trace_id|source_revision_hash|report_hash|evidence_hash|next_cursor/gi) ?? []
      ).length,
      owner_value_exposed: Boolean(input.owner) && html.includes(input.owner),
      supplied_secret_hit_count: suppliedSecrets.filter((secret) => html.includes(secret)).length,
      bearer_count: (html.match(/Bearer\s+\S+/gi) ?? []).length,
    };
  }, {
    status: expected.status ?? "",
    mode: expected.mode ?? "",
    owner: ownerId,
    password: config.adminPassword,
    canary: config.sensitiveCanary,
  });
  requireCondition(facts.heading_ok, "browser.report_dom", "heading");
  requireCondition(facts.summary_item_count === 4 && facts.summary_available, "browser.report_dom", "summary");
  requireCondition(facts.active_suite === expected.suite_kind, "browser.report_dom", "suite");
  requireCondition(
    facts.status_value === (expected.status ?? "") && facts.mode_value === (expected.mode ?? ""),
    "browser.report_dom",
    "controls",
  );
  requireCondition(facts.run_row_count === expected.entry_count, "browser.report_dom", "row_count");
  requireCondition(facts.status_rows_ok && facts.mode_rows_ok, "browser.report_dom", "row_semantics");
  requireCondition(
    expected.entry_count === 0 ? facts.empty_visible : !facts.empty_visible,
    "browser.report_dom",
    "empty_state",
  );
  requireCondition(facts.request_id_present, "browser.report_dom", "request_id");
  requireCondition(
    facts.dangerous_control_count === 0
      && facts.execution_command_count === 0
      && facts.script_count === 0
      && facts.callable_url_count === 0
      && facts.raw_field_name_count === 0
      && !facts.owner_value_exposed
      && facts.supplied_secret_hit_count === 0
      && facts.bearer_count === 0,
    "browser.report_dom",
    "privacy",
  );
  if (baselineSummary !== null) {
    requireCondition(facts.summary_signature === baselineSummary, "browser.report_dom", "summary_changed");
  }
  return facts.summary_signature;
}


/** 点击当前页第一条运行并验证真实详情 GET、Provider 分组、case 数量和详情 DOM 隐私。 */
async function exerciseDetail(page, config, ownerId) {
  const button = page.locator("[data-adapter-contract-detail]").first();
  await button.waitFor({ state: "visible", timeout: 30000 });
  const runId = await button.getAttribute("data-adapter-contract-detail");
  requireCondition(strictText(runId, RUN_ID), "browser.detail", "run_id");
  const responsePromise = waitForContractResponse(
    page,
    config,
    { kind: "detail", run_id: runId },
    200,
  );
  await button.click();
  const response = await responsePromise;
  const summary = await readDetailResponse(response, ownerId, runId);
  const detail = page.locator(".adapter-contract-detail");
  await detail.waitFor({ state: "visible", timeout: 30000 });
  const facts = await detail.evaluate((element) => ({
    case_count: element.querySelectorAll(".adapter-contract-case-row:not(.adapter-contract-case-head)").length,
    provider_count: element.querySelectorAll(".adapter-contract-provider-group").length,
    heading_ok: (element.textContent || "").includes("运行详情"),
    raw_field_name_count: (
      element.outerHTML.match(/tenant_id|source_trace_id|source_revision_hash|report_hash|evidence_hash|next_cursor/gi) ?? []
    ).length,
    callable_url_count: ((element.textContent || "").match(/https?:\/\//gi) ?? []).length,
  }));
  requireCondition(
    facts.case_count === summary.case_count
      && facts.provider_count === summary.provider_count
      && facts.heading_ok,
    "browser.detail_dom",
    "surface",
  );
  requireCondition(
    facts.raw_field_name_count === 0 && facts.callable_url_count === 0,
    "browser.detail_dom",
    "privacy",
  );
  await page.locator("[data-adapter-contract-close-detail]").click();
  await detail.waitFor({ state: "detached", timeout: 30000 });
  return { detail_confirmed: true, case_count: summary.case_count, provider_count: summary.provider_count };
}


/**
 * 验收默认摘要、真实 Control cursor 前后分页、详情、状态/模式筛选、suite 分段和真实空态。
 * 专用分页代理只把同源 limit 缩为 1，不伪造响应；空态使用不兼容 mode 的真实后端查询。
 */
async function exerciseReportSurface(page, config, identity, tracker) {
  const initialExpected = {
    kind: "list",
    suite_kind: "merge_gate",
    status: "",
    mode: "",
    cursor_present: false,
  };
  const initial = await runBrowserOperation(
    "browser.report.initial_list",
    () => performListAction(
      page,
      config,
      identity.owner_id,
      initialExpected,
      () => page.locator('[data-console-nav="adapter-contract-runs"]').click(),
    ),
  );
  requireCondition(initial.entry_count > 0, "browser.report", "merge_gate_missing");
  const baselineSummary = await runBrowserOperation(
    "browser.report.initial_dom",
    () => assertReportDom(
      page,
      { ...initialExpected, entry_count: initial.entry_count },
      identity.owner_id,
      config,
    ),
  );

  let previousPage;
  tracker.enablePaginationProxy();
  try {
    // 首个代理请求仍由页面发出生产合同 limit=50；route.fetch 只在同源后端读取时改成 limit=1。
    const firstPage = await runBrowserOperation(
      "browser.report.pagination_first",
      () => performListAction(
        page,
        config,
        identity.owner_id,
        initialExpected,
        () => page.locator('[data-console-nav="adapter-contract-runs"]').click(),
        1,
      ),
    );
    requireCondition(
      firstPage.entry_count === 1
        && firstPage.has_next_page
        && firstPage.first_run_id === initial.first_run_id,
      "browser.pagination",
      "first_page",
    );
    await runBrowserOperation(
      "browser.report.pagination_first_dom",
      () => assertReportDom(
        page,
        { ...initialExpected, entry_count: 1 },
        identity.owner_id,
        config,
        baselineSummary,
      ),
    );
    const nextExpected = { ...initialExpected, cursor_present: true };
    const nextPage = await runBrowserOperation(
      "browser.report.pagination_next",
      () => performListAction(
        page,
        config,
        identity.owner_id,
        nextExpected,
        () => page.locator("[data-adapter-contract-next]").click(),
        1,
      ),
    );
    requireCondition(nextPage.entry_count === 1, "browser.pagination", "next_page");
    await runBrowserOperation(
      "browser.report.pagination_next_dom",
      () => assertReportDom(
        page,
        { ...nextExpected, entry_count: 1 },
        identity.owner_id,
        config,
        baselineSummary,
      ),
    );
    const previousEnabled = await runBrowserOperation(
      "browser.report.pagination_previous_enabled",
      () => page.locator("[data-adapter-contract-previous]").isEnabled(),
    );
    requireCondition(
      previousEnabled,
      "browser.pagination",
      "previous_disabled",
    );
    previousPage = await runBrowserOperation(
      "browser.report.pagination_previous",
      () => performListAction(
        page,
        config,
        identity.owner_id,
        initialExpected,
        () => page.locator("[data-adapter-contract-previous]").click(),
        1,
      ),
    );
    requireCondition(
      previousPage.entry_count === 1 && previousPage.first_run_id === initial.first_run_id,
      "browser.pagination",
      "round_trip",
    );
    await runBrowserOperation(
      "browser.report.pagination_previous_dom",
      () => assertReportDom(
        page,
        { ...initialExpected, entry_count: 1 },
        identity.owner_id,
        config,
        baselineSummary,
      ),
    );
  } finally {
    // 任何成功或失败路径都关闭代理，避免后续筛选、故障或注销继续使用验收页大小。
    tracker.disablePaginationProxy();
  }
  requireCondition(
    tracker.paginationProxyRequestCount === 3 && tracker.paginationProxyFailureCount === 0,
    "browser.pagination_proxy",
    "request_count",
  );
  const detail = await runBrowserOperation(
    "browser.report.detail",
    () => exerciseDetail(page, config, identity.owner_id),
  );

  const mergeStatus = initial.first_status;
  requireCondition(RUN_STATUSES.has(mergeStatus), "browser.filters", "merge_status");
  const filteredMergeExpected = {
    kind: "list",
    suite_kind: "merge_gate",
    status: mergeStatus,
    mode: "mock",
    cursor_present: false,
  };
  const filteredMerge = await runBrowserOperation(
    "browser.report.filter_merge",
    async () => {
      await page.locator("#adapter-contract-status-filter").selectOption(mergeStatus);
      await page.locator("#adapter-contract-mode-filter").selectOption("mock");
      return performListAction(
        page,
        config,
        identity.owner_id,
        filteredMergeExpected,
        () => page.locator('#adapter-contract-filters button[type="submit"]').click(),
      );
    },
  );
  requireCondition(filteredMerge.entry_count > 0, "browser.filters", "merge_result");
  await runBrowserOperation(
    "browser.report.filter_merge_dom",
    () => assertReportDom(
      page,
      { ...filteredMergeExpected, entry_count: filteredMerge.entry_count },
      identity.owner_id,
      config,
      baselineSummary,
    ),
  );

  const resetMerge = await runBrowserOperation(
    "browser.report.reset_merge",
    () => performListAction(
      page,
      config,
      identity.owner_id,
      initialExpected,
      () => page.locator("[data-adapter-contract-reset]").click(),
    ),
  );
  await runBrowserOperation(
    "browser.report.reset_merge_dom",
    () => assertReportDom(
      page,
      { ...initialExpected, entry_count: resetMerge.entry_count },
      identity.owner_id,
      config,
      baselineSummary,
    ),
  );

  const probeExpected = {
    kind: "list",
    suite_kind: "probe",
    status: "",
    mode: "",
    cursor_present: false,
  };
  const probe = await runBrowserOperation(
    "browser.report.probe",
    () => performListAction(
      page,
      config,
      identity.owner_id,
      probeExpected,
      () => page.locator('[data-adapter-contract-suite="probe"]').click(),
    ),
  );
  requireCondition(probe.entry_count > 0, "browser.report", "probe_missing");
  await runBrowserOperation(
    "browser.report.probe_dom",
    () => assertReportDom(
      page,
      { ...probeExpected, entry_count: probe.entry_count },
      identity.owner_id,
      config,
      baselineSummary,
    ),
  );
  const probeStatus = probe.first_status;
  requireCondition(RUN_STATUSES.has(probeStatus), "browser.filters", "probe_status");
  const filteredProbeExpected = {
    ...probeExpected,
    status: probeStatus,
    mode: "gateway_probe",
  };
  const filteredProbe = await runBrowserOperation(
    "browser.report.filter_probe",
    async () => {
      await page.locator("#adapter-contract-status-filter").selectOption(probeStatus);
      await page.locator("#adapter-contract-mode-filter").selectOption("gateway_probe");
      return performListAction(
        page,
        config,
        identity.owner_id,
        filteredProbeExpected,
        () => page.locator('#adapter-contract-filters button[type="submit"]').click(),
      );
    },
  );
  requireCondition(filteredProbe.entry_count > 0, "browser.filters", "probe_result");
  await runBrowserOperation(
    "browser.report.filter_probe_dom",
    () => assertReportDom(
      page,
      { ...filteredProbeExpected, entry_count: filteredProbe.entry_count },
      identity.owner_id,
      config,
      baselineSummary,
    ),
  );

  const emptyExpected = { ...probeExpected, mode: "mock" };
  const empty = await runBrowserOperation(
    "browser.report.empty",
    async () => {
      await page.locator("#adapter-contract-status-filter").selectOption("");
      await page.locator("#adapter-contract-mode-filter").selectOption("mock");
      return performListAction(
        page,
        config,
        identity.owner_id,
        emptyExpected,
        () => page.locator('#adapter-contract-filters button[type="submit"]').click(),
      );
    },
  );
  requireCondition(empty.entry_count === 0, "browser.empty", "not_empty");
  await runBrowserOperation(
    "browser.report.empty_dom",
    () => assertReportDom(
      page,
      { ...emptyExpected, entry_count: 0 },
      identity.owner_id,
      config,
      baselineSummary,
    ),
  );

  const restored = await runBrowserOperation(
    "browser.report.restore",
    () => performListAction(
      page,
      config,
      identity.owner_id,
      initialExpected,
      () => page.locator('[data-adapter-contract-suite="merge_gate"]').click(),
    ),
  );
  requireCondition(restored.entry_count > 0, "browser.report", "restore_failed");
  await runBrowserOperation(
    "browser.report.restore_dom",
    () => assertReportDom(
      page,
      { ...initialExpected, entry_count: restored.entry_count },
      identity.owner_id,
      config,
      baselineSummary,
    ),
  );
  return {
    initial_entry_count: initial.entry_count,
    summary_confirmed: true,
    suite_status_mode_confirmed: true,
    pagination_confirmed: true,
    empty_state_confirmed: true,
    ...detail,
  };
}


/** 读取页面、侧栏、主区域、表格和按钮几何事实，不返回任何 DOM 文本。 */
async function readLayoutMetrics(page) {
  return page.evaluate(() => {
    const root = document.querySelector("[data-adapter-contract-root]");
    const sidebar = document.querySelector(".console-sidebar");
    const main = document.querySelector(".console-main");
    const rootRect = root?.getBoundingClientRect();
    const sidebarRect = sidebar?.getBoundingClientRect();
    const mainRect = main?.getBoundingClientRect();
    const overlapWidth = Math.max(0, Math.min(sidebarRect?.right ?? 0, mainRect?.right ?? 0)
      - Math.max(sidebarRect?.left ?? 0, mainRect?.left ?? 0));
    const overlapHeight = Math.max(0, Math.min(sidebarRect?.bottom ?? 0, mainRect?.bottom ?? 0)
      - Math.max(sidebarRect?.top ?? 0, mainRect?.top ?? 0));
    const tables = [...(root?.querySelectorAll(".adapter-contract-run-table,.adapter-contract-case-table") ?? [])];
    const visibleButtons = [...(root?.querySelectorAll("button") ?? [])].filter((button) => {
      const style = getComputedStyle(button);
      return style.display !== "none" && style.visibility !== "hidden" && button.getClientRects().length > 0;
    });
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      root_visible: Boolean(rootRect) && rootRect.width > 0 && rootRect.height > 0,
      root_horizontal_inside: Boolean(rootRect) && rootRect.left >= -1 && rootRect.right <= innerWidth + 1,
      root_scroll_width: root?.scrollWidth ?? 0,
      root_client_width: root?.clientWidth ?? 0,
      navigation_main_overlap_area: overlapWidth * overlapHeight,
      overflowing_table_count: tables.filter((table) => table.scrollWidth > table.clientWidth + 1).length,
      overflowing_button_count: visibleButtons.filter(
        (button) => button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1,
      ).length,
      empty_button_count: visibleButtons.filter((button) => !(button.textContent || "").trim()).length,
      visible_run_count: root?.querySelectorAll(".adapter-contract-run-row:not(.adapter-contract-run-head)").length ?? 0,
      summary_item_count: root?.querySelectorAll(".adapter-contract-summary > div").length ?? 0,
    };
  });
}


/** 校验双视口没有页面横溢、侧栏遮挡、表格溢出、按钮裁切或摘要缺项。 */
function assertLayoutMetrics(metrics, viewport) {
  const stage = "browser.layout." + viewport.name;
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
  requireCondition(metrics.overflowing_table_count === 0, stage, "table_overflow");
  requireCondition(metrics.overflowing_button_count === 0 && metrics.empty_button_count === 0, stage, "button_overflow");
  requireCondition(metrics.visible_run_count > 0 && metrics.summary_item_count === 4, stage, "surface_missing");
  return metrics;
}


/** 捕获固定视口，屏蔽用户菜单后执行布局、PNG 尺寸和非空像素检查。 */
async function captureViewport(page, config, viewport) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  await page.locator(".adapter-contract-heading").scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  const layout = assertLayoutMetrics(await readLayoutMetrics(page), viewport);
  const filename = "page-091-" + viewport.name + ".png";
  const target = path.join(config.artifactDirectory, "screenshots", filename);
  const buffer = await page.screenshot({
    path: target,
    type: "png",
    fullPage: false,
    animations: "disabled",
    mask: [page.locator(".user-menu")],
    maskColor: "#d1d5db",
  });
  const pixels = inspectPngPixels(buffer, viewport, "browser.screenshot." + viewport.name);
  return {
    viewport: viewport.name,
    filename,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    byte_length: buffer.length,
    ...pixels,
    layout,
  };
}


/**
 * 验证浏览器故障 DOM 的状态分流。
 * 403 必须清除旧授权事实；503 必须保留已有安全页为 partial，避免短暂存储故障让只读历史不可用。
 */
function validateFaultDomProjection(facts, expectedStatus) {
  requireCondition([403, 503].includes(expectedStatus), "browser.fault", "status");
  const commonFactsValid = facts?.error_visible === true && facts?.request_id_visible === true;
  const stateFactsValid = expectedStatus === 403
    ? facts?.table_count === 0 && facts?.refresh_count === 0 && facts?.detail_count === 0
    : facts?.table_count === 1 && facts?.refresh_count === 1 && facts?.detail_count > 0;
  requireCondition(commonFactsValid && stateFactsValid, "browser.fault", "error_dom");
  return true;
}


/** 验证一次性 403/503 投影、统一错误链路、错误 DOM 和下一次真实 200 恢复。 */
async function exerciseDeterministicFault(page, config, identity, tracker, status) {
  tracker.armFault(status);
  const expected = {
    kind: "list",
    suite_kind: "merge_gate",
    status: "",
    mode: "",
    cursor_present: false,
  };
  const currentRoot = page.locator("[data-adapter-contract-root]").first();
  const previousRoot = await currentRoot.elementHandle();
  const responsePromise = waitForContractResponse(page, config, expected, status);
  await page.locator('[data-console-nav="adapter-contract-runs"]').click();
  const response = await responsePromise;
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  validateUnifiedErrorPayload(payload, {
    get(name) {
      return response.headers()[String(name).toLowerCase()] ?? null;
    },
  }, status);
  const root = await waitForReportRootReplacement(page, previousRoot);
  const alert = root.locator('[role="alert"]');
  await alert.waitFor({ state: "visible", timeout: 30000 });
  const facts = await root.evaluate((element, expectedStatus) => {
    const text = element.textContent || "";
    return {
      error_visible: expectedStatus === 403
        ? text.includes("当前用户无权查看契约测试报告")
        : text.includes("契约测试运行暂不可用"),
      request_id_visible: text.includes("request_id: req_prd064_browser_fault_" + expectedStatus),
      table_count: element.querySelectorAll(".adapter-contract-run-table").length,
      refresh_count: element.querySelectorAll("[data-adapter-contract-refresh]").length,
      detail_count: element.querySelectorAll("[data-adapter-contract-detail]").length,
    };
  }, status);
  validateFaultDomProjection(facts, status);
  requireCondition(tracker.faultConsumed(status), "browser.fault", "not_consumed");
  tracker.closeFaultConsoleWindow(status);
  const recovered = await performListAction(
    page,
    config,
    identity.owner_id,
    expected,
    () => page.locator('[data-console-nav="adapter-contract-runs"]').click(),
  );
  requireCondition(recovered.entry_count > 0, "browser.fault", "recovery");
  await assertReportDom(
    page,
    { ...expected, entry_count: recovered.entry_count },
    identity.owner_id,
    config,
  );
  return true;
}


/** 扫描页面 URL、受保护 DOM 与 Storage；认证值只能位于专用 Session 槽位。 */
async function assertBrowserPrivacy(page, config, tracker) {
  const metrics = await page.evaluate(({ password, canary, authKeys, consoleOrigin }) => {
    const html = document.documentElement.outerHTML;
    const rootHtml = document.querySelector("[data-adapter-contract-root]")?.outerHTML || "";
    const localEntries = Object.entries(localStorage);
    const sessionEntries = Object.entries(sessionStorage);
    const sessionValue = sessionStorage.getItem("metahub.auth_token") || "";
    const nonValueEntries = sessionEntries.filter(([key]) => key !== "metahub.auth_token");
    const allStorage = JSON.stringify([...localEntries, ...sessionEntries]);
    const suppliedSecrets = [password, canary].filter(Boolean);
    return {
      location_contract: location.origin === consoleOrigin
        && location.pathname === "/"
        && location.search === ""
        && location.hash === "#adapter-contract-runs",
      auth_slot_count: sessionEntries.filter(([key]) => authKeys.includes(key)).length,
      unknown_session_slot_count: sessionEntries.filter(([key]) => !authKeys.includes(key)).length,
      forbidden_storage_key_count: [...localEntries, ...sessionEntries].filter(
        ([key]) => !authKeys.includes(key)
          && /password|token|secret|prompt|response|provider[_-]?key|endpoint/i.test(key),
      ).length,
      supplied_secret_hit_count: suppliedSecrets.filter(
        (secret) => [location.href, html, allStorage].some((value) => value.includes(secret)),
      ).length,
      session_value_outside_slot: Boolean(sessionValue) && [
        location.href,
        html,
        JSON.stringify(localEntries),
        JSON.stringify(nonValueEntries),
      ].some((value) => value.includes(sessionValue)),
      bearer_hit_count: [location.href, html, allStorage].reduce(
        (count, value) => count + (value.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
        0,
      ),
      forbidden_dom_field_count: (
        rootHtml.match(/tenant_id|source_trace_id|source_revision_hash|report_hash|evidence_hash|next_cursor/gi) ?? []
      ).length,
      callable_url_count: ((document.querySelector("[data-adapter-contract-root]")?.textContent || "")
        .match(/https?:\/\//gi) ?? []).length,
    };
  }, {
    password: config.adminPassword,
    canary: config.sensitiveCanary,
    authKeys: AUTH_STORAGE_KEYS,
    consoleOrigin: config.consoleOrigin,
  });
  requireCondition(metrics.location_contract, "browser.privacy", "location");
  requireCondition(metrics.auth_slot_count === AUTH_STORAGE_KEYS.length, "browser.privacy", "auth_slots");
  requireCondition(metrics.unknown_session_slot_count === 0, "browser.privacy", "unknown_session_slot");
  requireCondition(metrics.forbidden_storage_key_count === 0, "browser.privacy", "storage_key");
  requireCondition(metrics.supplied_secret_hit_count === 0, "browser.privacy", "secret_exposed");
  requireCondition(!metrics.session_value_outside_slot, "browser.privacy", "session_exposed");
  requireCondition(metrics.bearer_hit_count === 0, "browser.privacy", "bearer_exposed");
  requireCondition(
    metrics.forbidden_dom_field_count === 0 && metrics.callable_url_count === 0,
    "browser.privacy",
    "dom_exposed",
  );
  requireCondition(
    tracker.networkPrivacyViolationCount === 0 && tracker.consolePrivacyViolationCount === 0,
    "browser.privacy",
    "network_or_console",
  );
  return {
    auth_storage_contract_valid: true,
    location_contract_valid: true,
    sensitive_url_dom_storage_console_hits: 0,
  };
}


/** 执行真实注销，并确认认证槽位和 PAGE-091 受保护 DOM 已销毁。 */
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
      "[data-adapter-contract-root],.adapter-contract-page,.adapter-contract-runs-panel,.console-shell",
    ).length,
  }), AUTH_STORAGE_KEYS);
  requireCondition(cleared.auth_slot_count === 0, "browser.logout", "auth_slots");
  requireCondition(cleared.protected_dom_count === 0, "browser.logout", "protected_dom");
  // 最终证据只记录固定布尔结论，不保留槽位名称之外的认证信息。
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
      JSON.stringify(evidence, null, 2) + "\n",
      { encoding: "utf8", flag: "wx" },
    );
  } catch {
    throw new BrowserAcceptanceFailure("evidence.publish", "write_failed");
  }
}


/** 启动离线 Chromium，完成登录、真实报告流程、双视口、故障、隐私与注销验收。 */
async function executeAcceptance(config) {
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
      () => loginAdministrator(page, config),
    );
    const surface = await runBrowserOperation(
      "browser.report",
      () => exerciseReportSurface(page, config, identity, tracker),
    );
    const screenshots = [];
    screenshots.push(await runBrowserOperation(
      "browser.screenshot.desktop",
      () => captureViewport(page, config, VIEWPORTS[0]),
    ));
    screenshots.push(await runBrowserOperation(
      "browser.screenshot.mobile",
      () => captureViewport(page, config, VIEWPORTS[1]),
    ));
    await page.setViewportSize({ width: VIEWPORTS[0].width, height: VIEWPORTS[0].height });
    const permissionFault = await runBrowserOperation(
      "browser.fault.403",
      () => exerciseDeterministicFault(page, config, identity, tracker, 403),
    );
    const storageFault = await runBrowserOperation(
      "browser.fault.503",
      () => exerciseDeterministicFault(page, config, identity, tracker, 503),
    );
    const privacy = await runBrowserOperation(
      "browser.privacy",
      () => assertBrowserPrivacy(page, config, tracker),
    );
    requireCondition(tracker.blockedRequestCount === 0, "browser.network", "external_request");
    requireCondition(tracker.invalidContractRequestCount === 0, "browser.network", "contract_request");
    requireCondition(
      config.controlBackendOrigin === config.controlOrigin
        || (tracker.controlBackendProxyRequestCount > 0 && tracker.controlBackendProxyFailureCount === 0),
      "browser.network",
      "control_proxy",
    );
    requireCondition(tracker.pageErrorCount === 0, "browser.errors", "page_error");
    requireCondition(tracker.consoleErrorCount === 0, "browser.errors", "console_error");
    requireCondition(tracker.listRequestCount >= 13, "browser.report", "list_reads_missing");
    requireCondition(tracker.detailRequestCount >= 1, "browser.report", "detail_read_missing");
    requireCondition(
      tracker.loginStatuses.includes(200)
        && tracker.meStatuses.includes(200)
        && tracker.navigationStatuses.includes(200)
        && tracker.listStatuses.includes(200)
        && tracker.listStatuses.includes(403)
        && tracker.listStatuses.includes(503)
        && tracker.detailStatuses.includes(200),
      "browser.http",
      "status_missing",
    );
    requireCondition(
      screenshots.length === 2 && screenshots.every((item) => item.non_blank_pixels),
      "browser.screenshots",
      "blank_or_missing",
    );
    const logout = await runBrowserOperation(
      "browser.logout",
      () => logoutAndVerify(page, config),
    );
    requireCondition(tracker.logoutStatuses.includes(200), "browser.logout", "tracked_status");
    const evidence = {
      schema_version: 1,
      prd_id: "PRD-064",
      page_id: "PAGE-091",
      status: "passed",
      checks: {
        http_statuses: {
          login_200: true,
          identity_200: true,
          navigation_200: true,
          list_200: true,
          detail_200: true,
          permission_403: true,
          storage_503: true,
          logout_200: true,
        },
        administrator_confirmed: identity.administrator_confirmed,
        backend_navigation_confirmed: identity.navigation_owner_confirmed,
        report_surface: surface,
        deterministic_faults: {
          permission_projection_confirmed: permissionFault,
          storage_projection_confirmed: storageFault,
          recovery_confirmed: true,
        },
        privacy,
        logout,
        network: {
          external_request_count: 0,
          invalid_contract_request_count: 0,
          sensitive_request_hit_count: 0,
          pagination_request_count: 3,
          isolated_control_proxy_confirmed: config.controlBackendOrigin === config.controlOrigin
            || tracker.controlBackendProxyRequestCount > 0,
        },
        browser_errors: {
          page_error_count: 0,
          unexpected_console_error_count: 0,
          sensitive_console_hit_count: 0,
        },
        screenshots: { count: 2, all_non_blank: true },
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


/** CLI 仅输出固定成功摘要或脱敏 stage/code，证据路径、口令和 Session 值永不进入终端。 */
async function main() {
  try {
    const config = await readConfiguration();
    const evidence = await executeAcceptance(config);
    process.stdout.write(JSON.stringify({
      status: evidence.status,
      prd_id: evidence.prd_id,
      page_id: evidence.page_id,
      screenshot_count: evidence.screenshots.length,
    }) + "\n");
    return 0;
  } catch (error) {
    const failure = error instanceof BrowserAcceptanceFailure
      ? { stage: error.stage, code: error.code }
      : { stage: "browser.acceptance", code: "unexpected_failure" };
    process.stderr.write(JSON.stringify({ status: "failed", ...failure }) + "\n");
    return 1;
  }
}


export {
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  VIEWPORTS,
  adapterContractRequestDescriptor,
  assertLayoutMetrics,
  assertSafeEvidence,
  browserConsolePrivacyViolation,
  browserRequestAllowed,
  buildPaginationBackendUrl,
  buildBrowserEnvironment,
  classifyBrowserConsoleMessage,
  mapControlBackendUrl,
  normalizeLoopbackOrigin,
  pinBrowserWritableRoots,
  resolveArtifactDirectory,
  validateAdapterContractDetailPayload,
  validateAdapterContractListPayload,
  validateFaultDomProjection,
  validateNavigationOwner,
  validateUnifiedErrorPayload,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
