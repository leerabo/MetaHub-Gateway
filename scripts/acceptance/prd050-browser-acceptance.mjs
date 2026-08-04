/**
 * PRD-050 PAGE-072/PAGE-073 真实 Chrome 验收。
 *
 * 脚本只消费 ``artifacts/prd050/<batch>/runtime-state.json`` 及其同批次独立 SQLite，
 * 不伪造 RequestLog 或 CacheAffinity Selection。所有页面读取和配置写入均经过真实
 * Console/Control API；临时账号、Session 和配置变更先登记 owner，再在 finally 中按
 * tenant + 稳定对象 ID 收口。密码、Token、分类值和运行时内部 ID 只驻留进程内存。
 */

import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { link, mkdir, open, readFile, unlink, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { pathToFileURL } from "node:url";

import { inspectPngPixels } from "./prd047-browser-acceptance.mjs";

// 路径、认证槽、回环协议和输入格式常量共同限定验收只能访问当前工作区的本地批次。
const require = createRequire(import.meta.url);
// runner 固定以 D:\MetaHub 为 WorkingDirectory；Browser 必须从 cwd 解析并物理复核该根，
// 不能因脚本复制到候选目录而按 import.meta.url 上溯，也不能接受环境变量改写信任根。
const EXPECTED_WORKSPACE_ROOT = path.resolve("D:\\MetaHub");
const AUTH_TOKEN_KEY = "metahub.auth_token";
const AUTH_CONTEXT_KEY = "metahub.auth_context";
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const INTERNAL_PROTOCOLS = new Set(["about:", "blob:", "data:"]);
const REQUEST_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,160}$/;
const SAFE_SUFFIX_PATTERN = /^[a-z0-9][a-z0-9._-]{7,63}$/;
// generation 与批次目录、Runtime 状态严格同值；截图摘要只接受捕获缓冲计算的小写 SHA-256。
const GENERATION_PATTERN = /^\d{8}T\d{6}$/;
const MAX_STDIN_PASSWORD_BYTES = 4096;
const SHA256_HEX_PATTERN = /^[0-9a-f]{64}$/;
const SAFE_STAGE_PATTERN = /^[a-z0-9_.-]{1,120}$/;
const SAFE_CODE_PATTERN = /^[a-z0-9_]{1,80}$/;
const DATABASE_FILENAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.db$/;
const SENSITIVE_ENV_NAME = /(?:PASSWORD|TOKEN|SECRET|API_KEY|PROVIDER_KEY|VIRTUAL_KEY|PROMPT|RESPONSE)/i;
const FORBIDDEN_RUNTIME_KEY = /(?:password|access_token|plain(?:text)?_key|provider_key|prompt_text|response_text|webhook_secret|data_class|(?:^|_)digest(?:$|_))/i;
const GENERIC_SECRET_PATTERNS = Object.freeze([
  /\bBearer\s+[A-Za-z0-9._~+/-]{8,}/i,
  /\bmhv_[A-Za-z0-9_-]{9,}/,
  /\bsk[-_][A-Za-z0-9_-]{10,}/i,
  /\bwhsec_[A-Za-z0-9_-]{7,}/i,
  /prd050-(?:long|short|user|response)-[A-Za-z0-9_-]+/i,
]);
// after-commit 写探针禁止跟随重定向，避免凭据或 PATCH 正文离开已验证的 Control origin。
const GUARDED_ROUTE_FETCH_OPTIONS = Object.freeze({ timeout: 20_000, maxRedirects: 0 });
// 三视口与十种状态是 Browser 结果的闭集，任一状态或视口缺失都不能标记通过。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "narrow-desktop-900x800", width: 900, height: 800 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
const REQUIRED_VIEWPORT_STATES = Object.freeze([
  "loading",
  "empty",
  "configured",
  "editing",
  "saving",
  "conflict",
  "error",
  "page073_pending",
  "page073_warm",
  "page073_rebound",
]);
// 导航 ID 与真实页面标题必须集中维护；验收器不得在调用点散落可能漂移的 selector。
const NAVIGATION_TARGET_SELECTORS = Object.freeze({
  overview: "#dashboard-title",
  "routing-policies": "#routing-policies-title",
  "request-logs": "#request-logs-title",
});
// URL、Console 和 PAGE-073 状态枚举只识别固定安全字段，不把原始网络内容写入证据。
const SENSITIVE_URL_KEY = /(?:password|token|secret|api[_-]?key|provider[_-]?key|virtual[_-]?key|prompt|response|data[_-]?class|credential|digest)/i;
const EXPECTED_TRANSPORT_CONSOLE_ERROR = /(?:Failed to load resource|net::ERR_(?:FAILED|CONNECTION_FAILED|ABORTED))/i;
const PAGE073_EXPECTATIONS = Object.freeze({
  cache_affinity_created: Object.freeze({ state: "created", reason: "binding_created", estimatedHit: false }),
  cache_affinity_pending: Object.freeze({ state: "pending", reason: "binding_pending_reused", estimatedHit: false }),
  cache_affinity_warm: Object.freeze({ state: "warm", reason: "binding_warm_reused", estimatedHit: true }),
  cache_affinity_rebound: Object.freeze({ state: "rebound", reason: "binding_rebound", estimatedHit: false }),
});
// Runtime 请求键和 Selection DTO 字段使用精确白名单，阻止新增内部字段静默进入浏览器。
const REQUIRED_RUNTIME_REQUEST_KEYS = Object.freeze([
  ...Object.keys(PAGE073_EXPECTATIONS),
  "short_legacy",
]);
const SAFE_SELECTION_FIELDS = new Set([
  "cache_affinity_selection_id",
  "cache_affinity_id",
  "cache_affinity_version",
  "attempt_sequence",
  "selection_reason",
  "estimated_hit",
  "endpoint_preference_applied",
  "target_kind",
  "model_id",
  "provider_id",
  "provider_code",
  "created_at",
]);
// Browser 启动前至少复核五张 PRD-050 表，完整迁移行为另由迁移专项覆盖。
const REQUIRED_MIGRATION_TABLES = new Set([
  "cache_affinities",
  "cache_affinity_scopes",
  "cache_affinity_bindings",
  "cache_affinity_selections",
  "cache_affinity_config_events",
]);
// 这些关键触发器分别守住配置 owner、Binding/Selection 图和配置事件不可变性。
const REQUIRED_RUNTIME_TRIGGERS = new Set([
  "trg_cache_affinities_prd050_owner_insert",
  "trg_cache_affinity_scopes_prd050_pointer_update",
  "trg_cache_affinity_bindings_prd050_graph_insert",
  "trg_cache_affinity_bindings_prd050_transition_update",
  "trg_cache_affinity_selections_prd050_graph_insert",
  "trg_cache_affinity_selections_prd050_update",
  "trg_cache_affinity_config_events_prd050_graph_insert",
  "trg_cache_affinity_config_events_prd050_immutable_update",
]);

/** 只传播固定 stage/code，避免底层 URL、正文、Token 或调用栈进入验收日志。 */
class BrowserAcceptanceStageError extends Error {
  /** 根据白名单 stage/code 构造可公开错误；不接受底层 message 或 cause。 */
  constructor(stage, code) {
    const safeStage = SAFE_STAGE_PATTERN.test(String(stage)) ? String(stage) : "browser.unknown";
    const safeCode = SAFE_CODE_PATTERN.test(String(code)) ? String(code) : "stage_failed";
    super(`browser_acceptance_failed stage=${safeStage} code=${safeCode}`);
    this.name = "BrowserAcceptanceStageError";
    this.stage = safeStage;
    this.code = safeCode;
  }
}

/** 把任意异常压缩成固定阶段；单一已脱敏聚合异常保留其精确阶段。 */
function toSafeStageError(error, stage, fallbackCode = "stage_failed") {
  if (error instanceof BrowserAcceptanceStageError) return error;
  if (error instanceof AggregateError && error.errors.length === 1) {
    return toSafeStageError(error.errors[0], stage, fallbackCode);
  }
  if (
    error instanceof AggregateError
    && error.errors.length > 1
    && error.errors.every((item) => item instanceof BrowserAcceptanceStageError)
  ) {
    // 全部子错都已脱敏时保留确定的首错；只要混入一个 raw error 就继续降级 generic。
    return error.errors[0];
  }
  const code = String(error?.name) === "AssertionError"
    ? "assertion_failed"
    : String(error?.name) === "TimeoutError"
      ? "timeout"
      : fallbackCode;
  return new BrowserAcceptanceStageError(stage, code);
}

/** 执行有名阶段，并在异常离开当前边界前完成脱敏。 */
async function runAcceptanceStage(stage, action, fallbackCode = "stage_failed") {
  try {
    return await action();
  } catch (error) {
    // 公开边界只允许固定 stage/code，底层异常正文必须在离开当前阶段前丢弃。
    throw toSafeStageError(error, stage, fallbackCode);
  }
}

/** 判断 candidate 是否严格位于 root 内，防止参数把制品或数据库指向工作区外。 */
function pathIsInside(root, candidate, allowEqual = false) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return (allowEqual && relative === "")
    || (relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative));
}

/** 比较两个规范路径是否指向同一逻辑位置；双向 relative 避免前缀字符串误判。 */
function pathsReferToSameLocation(left, right) {
  const resolvedLeft = path.resolve(left);
  const resolvedRight = path.resolve(right);
  return path.relative(resolvedLeft, resolvedRight) === ""
    && path.relative(resolvedRight, resolvedLeft) === "";
}

/**
 * 从 runner 固定 WorkingDirectory 建立验收根合同。
 * 参数只允许单测注入 cwd 与只读文件系统函数；正式执行不读取任何可变环境根。
 * 返回逻辑工作区根和 PRD-050 制品根，且两者必须已存在、无 reparse 物理偏移。
 */
function resolveAcceptanceRoots({
  workingDirectory = process.cwd(),
  existsSyncImpl = existsSync,
  realpathImpl = realpathSync.native,
} = {}) {
  const workspaceRoot = path.resolve(workingDirectory);
  assert.ok(
    pathsReferToSameLocation(workspaceRoot, EXPECTED_WORKSPACE_ROOT),
    "Browser 验收必须由 runner 在固定工作区根启动",
  );
  const artifactRoot = path.resolve(workspaceRoot, "artifacts", "prd050");
  assert.ok(existsSyncImpl(workspaceRoot), "验收工作区根不存在");
  assert.ok(existsSyncImpl(artifactRoot), "PRD-050 制品根不存在");

  const expectedPhysicalRoot = path.resolve(realpathImpl(EXPECTED_WORKSPACE_ROOT));
  const workspacePhysicalRoot = path.resolve(realpathImpl(workspaceRoot));
  const artifactPhysicalRoot = path.resolve(realpathImpl(artifactRoot));
  assert.ok(
    pathsReferToSameLocation(expectedPhysicalRoot, EXPECTED_WORKSPACE_ROOT),
    "固定工作区根不得经过 junction 或 symlink",
  );
  assert.ok(
    pathsReferToSameLocation(workspacePhysicalRoot, expectedPhysicalRoot),
    "runner 工作目录的物理目标与固定工作区不一致",
  );
  assert.ok(
    pathsReferToSameLocation(artifactPhysicalRoot, artifactRoot),
    "PRD-050 制品根不得经过 junction 或 symlink",
  );
  assert.ok(
    pathIsInside(workspacePhysicalRoot, artifactPhysicalRoot),
    "PRD-050 制品根的物理目标越过工作区",
  );
  return Object.freeze({ workspaceRoot, artifactRoot });
}

/**
 * 向上寻找 candidate 最近的已存在父路径；仅用于在创建 browser/result 前解析真实物理边界。
 * 调用方可以在单测中注入 existsSync，避免合同测试依赖主机 junction 权限。
 */
function nearestExistingPath(candidate, existsSyncImpl = existsSync) {
  let current = path.resolve(candidate);
  while (!existsSyncImpl(current)) {
    const parent = path.dirname(current);
    assert.notEqual(parent, current, "验收路径没有可解析的已存在父目录");
    current = parent;
  }
  return current;
}

/**
 * 在 realpath 层验证 candidate 位于 root 内，防止词法安全路径通过 junction/symlink 写到批次外。
 * 对尚未创建的文件，以最近已存在父目录的 realpath 加剩余相对段计算最终物理目标。
 */
function assertPhysicalPathInside(
  root,
  candidate,
  {
    allowEqual = false,
    existsSyncImpl = existsSync,
    realpathImpl = realpathSync.native,
  } = {},
) {
  const lexicalRoot = path.resolve(root);
  const lexicalCandidate = path.resolve(candidate);
  const existingRoot = nearestExistingPath(lexicalRoot, existsSyncImpl);
  const existingCandidate = nearestExistingPath(lexicalCandidate, existsSyncImpl);
  assert.equal(existingRoot, lexicalRoot, "验收物理根目录必须已经存在");
  const physicalRoot = path.resolve(realpathImpl(existingRoot));
  const physicalExistingCandidate = path.resolve(realpathImpl(existingCandidate));
  const unresolvedSuffix = path.relative(existingCandidate, lexicalCandidate);
  assert.ok(!unresolvedSuffix.startsWith("..") && !path.isAbsolute(unresolvedSuffix));
  const physicalCandidate = path.resolve(physicalExistingCandidate, unresolvedSuffix);
  assert.ok(
    pathIsInside(physicalRoot, physicalCandidate, allowEqual),
    "验收路径的物理目标越过批次边界",
  );
  return physicalCandidate;
}

/** 将 IPv6 URL hostname 的方括号去掉，统一回环地址判定。 */
function normalizedHostname(target) {
  return String(target.hostname).replace(/^\[|\]$/g, "").toLowerCase();
}

/** 校验一个精确 HTTP 回环 origin；路径、query、hash 和 URL 凭据全部失败关闭。 */
function normalizeLoopbackOrigin(value, label) {
  const target = new URL(String(value));
  assert.equal(target.protocol, "http:", `${label} 必须使用 HTTP 回环地址`);
  assert.ok(LOOPBACK_HOSTS.has(normalizedHostname(target)), `${label} 必须是回环地址`);
  assert.equal(target.username, "");
  assert.equal(target.password, "");
  assert.equal(target.pathname, "/");
  assert.equal(target.search, "");
  assert.equal(target.hash, "");
  return target.origin;
}

/** 从已重定向标准输入读取唯一一行管理员口令，并拒绝空值、多行和超长输入。 */
function readAdminPasswordFromStdin(readStdin) {
  const source = String(readStdin() ?? "");
  assert.ok(Buffer.byteLength(source, "utf8") <= MAX_STDIN_PASSWORD_BYTES);
  assert.ok(/^[^\r\n]+(?:\r?\n)?$/.test(source), "标准输入必须且只能包含一行管理员密码");
  const password = source.replace(/\r?\n$/, "");
  assert.equal(/[\r\n]/.test(password), false, "标准输入必须且只能包含一行管理员密码");
  assert.ok(password.length >= 8, "标准输入管理员密码缺失或过短");
  return password;
}

/** 解析无凭据命令行参数；管理员密码只允许从标准输入进入进程内存。 */
function parseBrowserArgs(
  argv = process.argv.slice(2),
  env = process.env,
  acceptanceRoots = null,
  readStdin = () => readFileSync(0, "utf8"),
) {
  // 密码必须在任何路径校验或 Chrome 启动前从环境删除；后续只保留当前 Node 进程内副本，
  // 即使参数校验失败也不能让调用方随后启动的子进程继承该 Secret。
  delete env.METAHUB_PRD050_ADMIN_PASSWORD;
  delete env.METAHUB_ADMIN_PASSWORD;
  const adminPassword = readAdminPasswordFromStdin(readStdin);
  assert.ok(adminPassword.length >= 8, "标准输入管理员密码缺失或过短");
  // 根校验故意晚于密码环境变量删除：即使 cwd 或物理路径失败，也不能把 Secret 留给后续子进程。
  const roots = acceptanceRoots ?? resolveAcceptanceRoots();
  const workspaceRoot = path.resolve(String(roots?.workspaceRoot ?? ""));
  const artifactRoot = path.resolve(String(roots?.artifactRoot ?? ""));
  assert.ok(pathsReferToSameLocation(workspaceRoot, EXPECTED_WORKSPACE_ROOT));
  assert.ok(
    pathsReferToSameLocation(
      artifactRoot,
      path.resolve(workspaceRoot, "artifacts", "prd050"),
    ),
  );
  const values = new Map();
  const flags = new Set();
  for (let index = 0; index < argv.length; index += 1) {
    const name = String(argv[index]);
    if (["--confirm-local-state-mutation", "--password-stdin"].includes(name)) {
      flags.add(name);
      continue;
    }
    assert.ok(name.startsWith("--") && index + 1 < argv.length, "浏览器验收参数不完整");
    assert.ok(!values.has(name), "浏览器验收参数重复");
    values.set(name, String(argv[++index]));
  }
  assert.equal(flags.has("--password-stdin"), true, "必须显式声明 --password-stdin");
  assert.equal(values.has("--admin-password"), false, "禁止通过 --admin-password 传递明文");
  const batch = String(values.get("--batch") ?? env.METAHUB_PRD050_BATCH ?? "");
  const runtimeStateValue = values.get("--runtime-state")
    ?? env.METAHUB_PRD050_RUNTIME_STATE
    ?? (batch ? path.join(artifactRoot, batch, "runtime-state.json") : "");
  assert.ok(runtimeStateValue, "缺少 --runtime-state 或 METAHUB_PRD050_BATCH");
  const runtimeStatePath = path.resolve(runtimeStateValue);
  assert.ok(pathIsInside(artifactRoot, runtimeStatePath));
  const batchDirectory = path.dirname(runtimeStatePath);
  assert.equal(path.dirname(batchDirectory), artifactRoot);
  assert.equal(path.basename(runtimeStatePath), "runtime-state.json");
  const generation = String(values.get("--generation") ?? "");
  assert.ok(GENERATION_PATTERN.test(generation), "缺少或非法 --generation");
  assert.equal(generation, path.basename(batchDirectory), "--generation 必须与批次目录一致");
  // 重试只能写入同批次下的新目录；闭集名称禁止路径分隔符，也不会覆盖默认 browser 失败证据。
  const attempt = String(values.get("--attempt") ?? "");
  assert.ok(
    attempt === "" || /^[a-z][a-z0-9_]{0,31}$/.test(attempt),
    "浏览器验收 attempt 名称非法",
  );
  const browserDirectoryName = attempt ? `browser-${attempt}` : "browser";
  const browserDirectory = path.resolve(batchDirectory, browserDirectoryName);
  const resultPath = path.resolve(
    values.get("--result-output") ?? path.join(browserDirectory, "browser-result.json"),
  );
  assert.ok(pathIsInside(batchDirectory, browserDirectory));
  assert.ok(pathIsInside(browserDirectory, resultPath));
  assert.equal(path.extname(resultPath), ".json");
  const confirmed = flags.has("--confirm-local-state-mutation")
    || env.METAHUB_CONFIRM_BROWSER_STATE_MUTATION === "1";
  assert.equal(confirmed, true, "必须显式确认独立验收状态 mutation");
  return {
    workspaceRoot,
    artifactRoot,
    generation,
    runtimeStatePath,
    batchDirectory,
    browserDirectory,
    resultPath,
    controlOrigin: normalizeLoopbackOrigin(
      values.get("--control-url") ?? env.METAHUB_PRD050_CONTROL_URL ?? "http://127.0.0.1:18001",
      "Control URL",
    ),
    gatewayOrigin: normalizeLoopbackOrigin(
      values.get("--gateway-url") ?? env.METAHUB_PRD050_GATEWAY_URL ?? "http://127.0.0.1:18000",
      "Gateway URL",
    ),
    consoleOrigin: normalizeLoopbackOrigin(
      values.get("--console-url") ?? env.METAHUB_PRD050_CONSOLE_URL ?? "http://127.0.0.1:15173",
      "Console URL",
    ),
    tenantSlug: String(values.get("--tenant-slug") ?? env.METAHUB_TENANT_SLUG ?? "default"),
    adminEmail: String(
      values.get("--admin-email") ?? env.METAHUB_ADMIN_EMAIL ?? "admin@metahub.local",
    ).trim().toLowerCase(),
    adminPassword,
    chromePath: String(
      env.METAHUB_CHROME_PATH ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    ),
    headed: env.METAHUB_BROWSER_HEADED === "1",
  };
}

/** 递归拒绝 runtime-state 中的正文、凭据、分类值和摘要字段。 */
function assertRuntimeStateKeyPrivacy(value, parts = []) {
  if (!value || typeof value !== "object") return true;
  for (const [key, child] of Object.entries(value)) {
    assert.ok(!FORBIDDEN_RUNTIME_KEY.test(key), `runtime-state 敏感字段 ${[...parts, key].join(".")}`);
    assertRuntimeStateKeyPrivacy(child, [...parts, key]);
  }
  return true;
}

/** 校验稳定 ID，不在失败信息中回显不可信原值。 */
function requiredResourceId(value, label) {
  assert.ok(RESOURCE_ID_PATTERN.test(String(value ?? "")), `${label} 标识符非法`);
  return String(value);
}

/**
 * 校验 Runtime 通过状态，并强制要求真实 Gateway 生成的 created/pending/warm/rebound/short 五条请求。
 * 任一 ID 缺失或复用都会失败关闭，浏览器脚本不会通过 SQLite 合成替代事实。
 */
function validateRuntimeStateDocument(body) {
  assert.ok(body && typeof body === "object" && !Array.isArray(body));
  assertRuntimeStateKeyPrivacy(body);
  assert.equal(body.schema_version, 1);
  assert.equal(body.status, "passed");
  assert.equal(body.prd_id, "PRD-050");
  const generation = String(body.generation ?? "");
  assert.ok(GENERATION_PATTERN.test(generation));
  assert.equal(body.lifecycle?.credentials_cleanup_completed, true);
  assert.equal(body.lifecycle?.credentials_cleanup_verified, true);
  const suffix = String(body.suffix ?? "");
  assert.ok(SAFE_SUFFIX_PATTERN.test(suffix));
  const requestIds = {};
  for (const key of REQUIRED_RUNTIME_REQUEST_KEYS) {
    const requestId = String(body.request_ids?.[key] ?? "");
    assert.ok(REQUEST_ID_PATTERN.test(requestId), `runtime 缺少 ${key}`);
    requestIds[key] = requestId;
  }
  assert.equal(new Set(Object.values(requestIds)).size, REQUIRED_RUNTIME_REQUEST_KEYS.length);
  for (const check of [
    "migration_0040_applied",
    "admin_patch",
    "developer_read_only",
    "viewer_read_denied",
    "viewer_write_denied_before_body",
    "conflict_refresh_via_same_tenant_get",
    "request_log_selection_safe_fields",
    "provider_cache_tokens_not_derived_from_estimate",
    "database_sensitive_hits",
    "log_sensitive_hits",
    "foreign_key_check_clean",
    "integrity_check_ok",
  ]) {
    assert.ok(Object.hasOwn(body.checks ?? {}, check), `runtime 缺少 ${check}`);
  }
  assert.equal(body.checks.migration_0040_applied, true);
  assert.equal(body.checks.admin_patch, true);
  assert.equal(body.checks.developer_read_only, true);
  assert.equal(body.checks.viewer_read_denied, true);
  assert.equal(body.checks.viewer_write_denied_before_body, true);
  assert.equal(body.checks.conflict_refresh_via_same_tenant_get, true);
  assert.equal(body.checks.request_log_selection_safe_fields, true);
  assert.equal(body.checks.provider_cache_tokens_not_derived_from_estimate, true);
  assert.equal(body.checks.database_sensitive_hits, 0);
  assert.equal(body.checks.log_sensitive_hits, 0);
  assert.equal(body.checks.foreign_key_check_clean, true);
  assert.equal(body.checks.integrity_check_ok, true);
  return {
    generation,
    suffix,
    databaseFile: String(body.database_file ?? ""),
    tenantId: requiredResourceId(body.resource_ids?.main_tenant_id, "mainTenantId"),
    aliasId: requiredResourceId(body.resource_ids?.alias_id, "aliasId"),
    modelId: requiredResourceId(body.resource_ids?.model_a_id, "modelId"),
    requestIds,
  };
}

/** 读取 UTF-8/BOM Runtime JSON，并只返回浏览器所需的白名单投影。 */
async function loadRuntimeState(filePath) {
  const source = (await readFile(filePath, "utf8")).replace(/^\uFEFF/, "");
  return validateRuntimeStateDocument(JSON.parse(source));
}

/** 根据 runtime 白名单文件名解析同批次 SQLite，禁止绝对路径和嵌套目录。 */
function resolveRuntimeDatabasePath(batchDirectory, databaseFile) {
  const filename = String(databaseFile ?? "");
  assert.ok(DATABASE_FILENAME_PATTERN.test(filename));
  assert.equal(path.basename(filename), filename);
  const result = path.resolve(batchDirectory, filename);
  assert.ok(pathIsInside(batchDirectory, result));
  return result;
}

/** 断言 set 与 expected 完全一致，用于迁移对象和 Selection DTO 的闭集门禁。 */
function assertExactSet(actual, expected, label) {
  assert.equal(actual.size, expected.size, `${label} 数量不一致`);
  for (const value of expected) assert.ok(actual.has(value), `${label} 缺少对象`);
}

/** 从独立库读取 PAGE-072 原配置和 PAGE-073 真实请求事实；连接始终 query_only。 */
function loadStableDatabaseFacts(databasePath, runtime, adminEmail) {
  const database = new DatabaseSync(databasePath, { readOnly: true });
  try {
    database.exec("PRAGMA query_only=ON");
    const migrationVersion = Number(
      database.prepare("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").get().version,
    );
    assert.ok(migrationVersion >= 40);
    const tables = new Set(database.prepare(
      "SELECT name FROM sqlite_master WHERE type='table'",
    ).all().map((row) => String(row.name)).filter((name) => REQUIRED_MIGRATION_TABLES.has(name)));
    const triggers = new Set(database.prepare(
      "SELECT name FROM sqlite_master WHERE type='trigger'",
    ).all().map((row) => String(row.name)).filter((name) => REQUIRED_RUNTIME_TRIGGERS.has(name)));
    assertExactSet(tables, REQUIRED_MIGRATION_TABLES, "PRD-050 table");
    assertExactSet(triggers, REQUIRED_RUNTIME_TRIGGERS, "PRD-050 trigger");
    const tenant = database.prepare(
      "SELECT tenant_id, status FROM tenants WHERE tenant_id=?",
    ).get(runtime.tenantId);
    assert.ok(tenant && tenant.status === "active");
    const admin = database.prepare(
      "SELECT user_id, tenant_id, email, role, status FROM users WHERE tenant_id=? AND email=?",
    ).get(runtime.tenantId, adminEmail);
    assert.ok(admin && admin.role === "admin" && admin.status === "active");
    const alias = database.prepare(`
      SELECT a.model_alias_id, a.tenant_id, a.alias, a.environment_id, a.status,
             ca.cache_affinity_id, ca.status AS affinity_status, ca.data_class,
             ca.minimum_prefix_chars, ca.ttl_seconds, ca.version,
             ca.created_at, ca.updated_at
      FROM model_aliases a
      JOIN cache_affinities ca
        ON ca.tenant_id=a.tenant_id AND ca.model_alias_id=a.model_alias_id
      WHERE a.tenant_id=? AND a.model_alias_id=?
    `).get(runtime.tenantId, runtime.aliasId);
    assert.ok(alias && alias.cache_affinity_id && Number(alias.version) >= 1);
    const page073 = {};
    for (const [requestKey, expectation] of Object.entries(PAGE073_EXPECTATIONS)) {
      const requestId = runtime.requestIds[requestKey];
      const row = database.prepare(`
        SELECT s.request_id, s.selection_reason, s.estimated_hit, s.outcome_status,
               u.cache_tokens
        FROM cache_affinity_selections s
        JOIN request_logs l ON l.tenant_id=s.tenant_id AND l.request_id=s.request_id
        LEFT JOIN usage_ledger u ON u.tenant_id=s.tenant_id AND u.request_id=s.request_id
        WHERE s.tenant_id=? AND s.request_id=?
        ORDER BY s.attempt_sequence
      `).get(runtime.tenantId, requestId);
      assert.ok(row);
      assert.equal(row.selection_reason, expectation.reason);
      assert.equal(Boolean(row.estimated_hit), expectation.estimatedHit);
      assert.equal(row.outcome_status, "succeeded");
      assert.ok(Number.isSafeInteger(Number(row.cache_tokens)) && Number(row.cache_tokens) >= 0);
      page073[requestKey] = { ...expectation, cacheTokens: Number(row.cache_tokens) };
    }
    const shortCount = Number(database.prepare(`
      SELECT COUNT(*) AS total FROM cache_affinity_selections
      WHERE tenant_id=? AND request_id=?
    `).get(runtime.tenantId, runtime.requestIds.short_legacy).total);
    assert.equal(shortCount, 0);
    assertCacheEvidenceIndependent({
      created: page073.cache_affinity_created,
      warm: page073.cache_affinity_warm,
    });
    return {
      adminUserId: String(admin.user_id),
      aliasName: String(alias.alias),
      environmentId: String(alias.environment_id ?? ""),
      originalConfig: {
        status: String(alias.affinity_status),
        data_class: alias.data_class === null ? null : String(alias.data_class),
        minimum_prefix_chars: Number(alias.minimum_prefix_chars),
        ttl_seconds: Number(alias.ttl_seconds),
        version: Number(alias.version),
      },
      page073,
      migrationVersion,
    };
  } finally {
    database.close();
  }
}

/** 冻结预计复用与 Provider 实报缓存 Token 的两组反例，禁止前端互相推导。 */
function assertCacheEvidenceIndependent({ created, warm }) {
  assert.equal(created?.estimatedHit, false);
  assert.ok(Number(created?.cacheTokens) > 0);
  assert.equal(warm?.estimatedHit, true);
  assert.equal(Number(warm?.cacheTokens), 0);
  return true;
}

/** 收集配置密码、运行 Token 和敏感环境值；集合只用于内存等值扫描。 */
function collectConfiguredSecrets(values = [], env = process.env) {
  const secrets = new Set(values.map(String).filter((value) => value.length >= 4));
  for (const [name, value] of Object.entries(env)) {
    if (SENSITIVE_ENV_NAME.test(name) && String(value ?? "").length >= 4) {
      secrets.add(String(value));
    }
  }
  // 集合只驻内存参与等值扫描，调用方不得把内容序列化进最终结果。
  return secrets;
}

/** 判断文本是否包含任一精确 Secret 或通用 Key/Prompt/Response 哨兵。 */
function containsSecret(value, exactSecrets = []) {
  const text = String(value ?? "");
  return [...exactSecrets]
    .map(String)
    .filter((item) => item.length >= 4)
    .some((secret) => text.includes(secret))
    || GENERIC_SECRET_PATTERNS.some((pattern) => pattern.test(text));
}

/** 对 URL 做有界解码；坏 percent 编码本身就是隐私门禁失败，不能退回原文继续放行。 */
function decodeUrlComponent(value) {
  try {
    return decodeURIComponent(String(value ?? ""));
  } catch {
    assert.fail("浏览器 URL 包含非法编码");
  }
}

/**
 * 核对 URL 只指向允许 origin，且 query/hash 不承载凭据、分类、正文或摘要字段。
 * forbiddenValues 专门承载允许出现在 PAGE-072 DOM、但绝不能进入 URL 的 data_class。
 */
function assertUrlPrivacy(
  rawUrl,
  {
    allowedOrigins = [],
    exactSecrets = [],
    forbiddenValues = [],
  } = {},
) {
  const target = new URL(String(rawUrl));
  if (allowedOrigins.length) assert.ok(allowedOrigins.includes(target.origin), "浏览器 URL origin 越界");
  assert.equal(target.username, "");
  assert.equal(target.password, "");
  for (const [key, value] of target.searchParams) {
    assert.doesNotMatch(key, SENSITIVE_URL_KEY, "浏览器 URL query 含敏感字段名");
    assert.equal(containsSecret(value, exactSecrets), false, "浏览器 URL query 含敏感值");
  }
  const decodedHash = decodeUrlComponent(target.hash);
  assert.doesNotMatch(decodedHash, SENSITIVE_URL_KEY, "浏览器 URL hash 含敏感字段名");
  const decodedSurface = [decodeUrlComponent(target.pathname), decodeUrlComponent(target.search), decodedHash].join("\n");
  assert.equal(containsSecret(decodedSurface, exactSecrets), false, "浏览器 URL 含敏感值");
  for (const value of forbiddenValues) {
    const forbidden = String(value ?? "");
    if (forbidden.length >= 1) assert.equal(decodedSurface.includes(forbidden), false, "浏览器 URL 含受限业务值");
  }
  return { origin: target.origin, urlSensitiveHitCount: 0 };
}

/**
 * 核对 DOM、URL 和 Storage 隐私；登录态只豁免唯一 sessionStorage Token 槽，
 * 注销态要求两个认证槽同时消失；登录态 auth_context 必须精确属于当前 tenant/user/role/session。
 */
function assertSensitiveSurfaceSnapshot(
  snapshot,
  {
    exactSecrets = [],
    forbiddenUrlValues = [],
    allowedOrigins = [],
    activeToken = null,
    expectedOwner = null,
    loggedOut = false,
    label = "browser",
  } = {},
) {
  const localEntries = new Map(snapshot.localStorageEntries ?? []);
  const sessionEntries = new Map(snapshot.sessionStorageEntries ?? []);
  const publicValues = [
    snapshot.domText,
    snapshot.domHtml,
    ...localEntries.keys(),
    ...localEntries.values(),
  ];
  assert.ok(publicValues.every((value) => !containsSecret(value, exactSecrets)), `${label} 公开表面泄露敏感值`);
  assertUrlPrivacy(snapshot.url, { allowedOrigins, exactSecrets, forbiddenValues: forbiddenUrlValues });
  assert.ok(!localEntries.has(AUTH_TOKEN_KEY) && !localEntries.has(AUTH_CONTEXT_KEY));
  if (loggedOut) {
    assert.ok(!sessionEntries.has(AUTH_TOKEN_KEY) && !sessionEntries.has(AUTH_CONTEXT_KEY));
  } else {
    assert.ok(typeof activeToken === "string" && activeToken.length >= 16);
    assert.equal(sessionEntries.get(AUTH_TOKEN_KEY), activeToken);
    assert.ok(sessionEntries.has(AUTH_CONTEXT_KEY));
    assert.ok(expectedOwner && typeof expectedOwner === "object");
    let context;
    try {
      context = JSON.parse(String(sessionEntries.get(AUTH_CONTEXT_KEY)));
    } catch {
      assert.fail("浏览器认证上下文不是合法 JSON");
    }
    assert.ok(
      context?.user?.tenant_id === expectedOwner.tenantId
        && context?.user?.user_id === expectedOwner.userId
        && context?.user?.email === expectedOwner.email
        && context?.user?.role === expectedOwner.role
        && context?.session?.session_id === expectedOwner.sessionId,
      "浏览器认证上下文 owner 不一致",
    );
  }
  for (const [key, value] of sessionEntries) {
    // 唯一 Token 槽按 activeToken 精确比对后跳过通用 Secret 扫描，其他槽没有豁免。
    if (!loggedOut && key === AUTH_TOKEN_KEY) continue;
    assert.ok(!containsSecret(key, exactSecrets) && !containsSecret(value, exactSecrets));
  }
  const authSlotCount = [...sessionEntries.keys()]
    .filter((key) => key === AUTH_TOKEN_KEY || key === AUTH_CONTEXT_KEY).length;
  assert.equal(authSlotCount, loggedOut ? 0 : 2);
  return {
    domSensitiveHitCount: 0,
    storageSensitiveHitCount: 0,
    urlSensitiveHitCount: 0,
    authStorageContractValid: true,
    authOwnerContractValid: true,
  };
}

/** 把请求头名称归一为小写；值仅参与网络门禁，不进入结果制品。 */
function normalizedHeaders(headers = {}) {
  return Object.fromEntries(
    Object.entries(headers).map(([key, value]) => [String(key).toLowerCase(), String(value)]),
  );
}

/**
 * 对浏览器请求执行精确 origin 门禁。
 * Console/Control 只允许调用方配置的回环 origin；Gateway 只允许无凭据健康 GET/OPTIONS。
 */
function decideBrowserNetworkRequest(
  { url, method = "GET", headers = {} },
  { consoleOrigin, controlOrigin, gatewayOrigin },
) {
  let target;
  try {
    target = new URL(String(url));
  } catch {
    return { allowed: false, destination: "blocked", violation: "invalid_url" };
  }
  if (INTERNAL_PROTOCOLS.has(target.protocol)) {
    return { allowed: true, destination: "browser_internal", violation: null };
  }
  const loopback = target.protocol === "http:"
    && LOOPBACK_HOSTS.has(normalizedHostname(target))
    && target.username === ""
    && target.password === "";
  const requestHeaders = normalizedHeaders(headers);
  if (loopback && target.origin === consoleOrigin) {
    return { allowed: true, destination: "console", violation: null };
  }
  if (loopback && target.origin === controlOrigin) {
    // 四个目标 API 都是从 Console 发起的跨 origin fetch。只看目标 URL 会让伪造或缺失
    // Origin 的响应混入 CORS 成功证据，因此在网络放行点先执行请求侧精确匹配。
    if (
      classifyTargetApi(target.href, controlOrigin)
      && requestHeaders.origin !== consoleOrigin
    ) {
      return { allowed: false, destination: "blocked", violation: "target_origin" };
    }
    return { allowed: true, destination: "control", violation: null };
  }
  const health = new Set(["/health", "/health/live", "/health/ready", "/version"]);
  const noCredential = !requestHeaders.authorization && !requestHeaders.cookie;
  if (
    loopback
    && target.origin === gatewayOrigin
    && noCredential
    && health.has(target.pathname)
    && ["GET", "OPTIONS"].includes(String(method).toUpperCase())
  ) {
    return { allowed: true, destination: "gateway_health", violation: null };
  }
  return {
    allowed: false,
    destination: "blocked",
    violation: target.origin === gatewayOrigin ? "gateway_scope" : "origin",
  };
}

/** 将目标 API URL 分类为固定枚举，禁止把 query、Alias ID 或 request_id 写入网络证据。 */
function classifyTargetApi(rawUrl, controlOrigin) {
  let target;
  try {
    target = new URL(String(rawUrl));
  } catch {
    return null;
  }
  if (target.origin !== controlOrigin) return null;
  // 只把四个 PRD-050 页面接口归入目标集合，认证和其他控制面接口不混入统计。
  if (target.pathname === "/model-aliases") return "model_alias_list";
  if (/^\/model-aliases\/[^/]+\/cache-affinity$/.test(target.pathname)) return "cache_config_write";
  if (target.pathname === "/request-logs") return "request_log_list";
  if (/^\/request-logs\/[^/]+$/.test(target.pathname)) return "request_log_detail";
  return null;
}

/** 创建 Promise 闸门，验收只保存 resolve 函数，不把网络正文挂到共享状态。 */
function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

/**
 * 立即给可能 reject 的 Playwright waiter 安装 observer，并把结果压成只 resolve 的二态对象。
 * 页面 action 先失败时 waiter 仍会被观察，不会在 Context close 后变成游离 unhandled rejection。
 */
function observePromiseOutcome(promise) {
  assert.ok(promise && typeof promise.then === "function", "缺少待观察 Promise");
  return Promise.resolve(promise).then(
    (value) => ({ status: "fulfilled", value }),
    (error) => ({ status: "rejected", error }),
  );
}

/** 从已观察 outcome 取回成功值；失败仍在当前受控阶段抛出，随后由安全边界统一脱敏。 */
function unwrapPromiseOutcome(outcome) {
  assert.ok(outcome && ["fulfilled", "rejected"].includes(outcome.status));
  // 这里只恢复进程内异常控制流，不会把底层 error 写入日志或稳定制品。
  if (outcome.status === "rejected") throw outcome.error;
  return outcome.value;
}

/**
 * 执行一次浏览器动作并消费预先观察的 response waiter；动作失败也必须等 waiter 有界收敛。
 * 这样 click 异常不会把晚到的 Playwright TimeoutError 留到 Context close 或外层 cleanup 之后。
 */
async function runBrowserActionWithObservedResponse(action, responseOutcome) {
  assert.equal(typeof action, "function", "浏览器动作非函数");
  let actionFailure = null;
  try {
    await action();
  } catch (error) {
    actionFailure = error;
  }
  const outcome = await responseOutcome;
  // 动作错误优先，因为它是 waiter 未命中的直接原因；二者都已被 observer 消费。
  if (actionFailure) throw actionFailure;
  return unwrapPromiseOutcome(outcome);
}

/**
 * 创建每个角色独立的 Route 监督器。
 *
 * callback 异常只记录首个固定 stage/code；角色注销后必须等待 active=0 再封口，因而不会让
 * 页面 action 与外层 SQLite cleanup 并发。封口后的关闭期请求只会被中断，不再制造 shutdown 假阳性。
 */
function createBrowserRouteFailureSupervisor() {
  let activeCount = 0;
  let firstFailure = null;
  let sealed = false;
  const firstFailureSignal = createDeferred();
  const quiescenceWaiters = [];
  const sealHooks = new Set();
  return {
    /**
     * callback 开始时一律登记；allowed=false 表示封口后的请求只能中断。
     * 关闭期 callback 也计入 activeCount，避免 post-close 静止检查在 abort 尚未返回时假通过。
     */
    enter() {
      activeCount += 1;
      const allowed = !sealed;
      let finished = false;
      return {
        allowed,
        finish() {
          if (finished) return;
          finished = true;
          activeCount -= 1;
          if (activeCount === 0) {
            for (const resolve of quiescenceWaiters.splice(0)) resolve();
          }
        },
      };
    },
    /** 只保存脱敏异常对象；底层 cause/message/stack 从不进入监督器。 */
    fail(stage) {
      if (!firstFailure) {
        firstFailure = new BrowserAcceptanceStageError(stage, "route_handler_failed");
        // 信号只 resolve 安全错误对象，不 reject；即使尚无 waiter 也不会制造 unhandled rejection。
        firstFailureSignal.resolve(firstFailure);
      }
    },
    /**
     * 等待 gate 信号，同时监听首个 Route 失败并施加硬超时。
     * 裸 deferred 不得直接暴露给页面流程，否则 handler 在 resolve 前失败会让角色永久悬挂。
     */
    async waitForSignal(signal, stage, timeoutMillis = 30_000) {
      assert.ok(signal && typeof signal.then === "function", "缺少 gate Promise");
      assert.ok(SAFE_STAGE_PATTERN.test(String(stage)), "gate stage 非安全值");
      assert.ok(Number.isInteger(timeoutMillis) && timeoutMillis >= 1 && timeoutMillis <= 30_000);
      let timeout;
      const outcome = await Promise.race([
        Promise.resolve(signal).then((value) => ({ kind: "signal", value })),
        firstFailureSignal.promise.then((error) => ({ kind: "route_failure", error })),
        new Promise((resolve) => {
          timeout = setTimeout(() => resolve({ kind: "timeout" }), timeoutMillis);
        }),
      ]);
      clearTimeout(timeout);
      // Route 首错优先于 gate 超时返回，调用方据此进入统一 seal/close 收口。
      if (outcome.kind === "route_failure") throw outcome.error;
      if (outcome.kind === "timeout") {
        // 未命中真实请求同样必须有界失败，不能让角色与外层 owner cleanup 永久互相等待。
        throw new BrowserAcceptanceStageError(stage, "timeout");
      }
      return outcome.value;
    },
    /**
     * 登记封口释放钩子；仅用于唤醒仍在 gate 内的 callback，使其能在 Context 关闭前收敛。
     * 钩子必须同步、幂等且不得执行网络或数据库副作用。
     */
    registerSealHook(hook) {
      assert.equal(typeof hook, "function", "封口钩子非函数");
      if (sealed) {
        try { hook(); } catch { /* 封口钩子异常不得覆盖首个安全失败。 */ }
        return () => {};
      }
      sealHooks.add(hook);
      return () => sealHooks.delete(hook);
    },
    /**
     * 把 page.unroute 等 teardown Promise 纳入同一 active 账本。
     * 外层有界等待即使先超时，post-close 无限 drain 仍会等真实副作用结束后才放行 cleanup。
     */
    trackOperation(action) {
      assert.equal(typeof action, "function", "受控 Route 操作非函数");
      const ticket = this.enter();
      if (!ticket.allowed) {
        // Context 关闭后的迟到卸载由 close 本身负责；禁止在 post-close waiter 已唤醒后再新增副作用。
        ticket.finish();
        return Promise.resolve();
      }
      // action 必须在当前 JS turn 立即启动。若先用 Promise.then 延后一拍，新的 Route callback
      // 可能在“确认 active=0”和真正 page.unroute 之间进入，重新制造自动推进与二次 terminal 竞态。
      let operation;
      try {
        operation = action();
      } catch (error) {
        ticket.finish();
        return Promise.reject(error);
      }
      return Promise.resolve(operation).finally(() => ticket.finish());
    },
    /** 等待已登记 callback 全部返回，确保后续 cleanup 不与页面任务并发。 */
    async waitForQuiescence(timeoutMillis = 10_000) {
      assert.ok(
        timeoutMillis === null
          || (Number.isInteger(timeoutMillis) && timeoutMillis >= 1 && timeoutMillis <= 30_000),
      );
      if (activeCount === 0) return;
      await new Promise((resolve, reject) => {
        let completed = false;
        let timeout = null;
        const complete = () => {
          if (completed) return;
          completed = true;
          if (timeout) clearTimeout(timeout);
          resolve();
        };
        quiescenceWaiters.push(complete);
        if (timeoutMillis === null) return;
        timeout = setTimeout(() => {
          if (completed) return;
          completed = true;
          const index = quiescenceWaiters.indexOf(complete);
          if (index >= 0) quiescenceWaiters.splice(index, 1);
          reject(new BrowserAcceptanceStageError("browser.route.quiescence", "timeout"));
        }, timeoutMillis);
      });
    },
    /** 在受控角色生命周期内重新抛出首个安全失败。 */
    throwIfFailed() {
      if (firstFailure) throw firstFailure;
    },
    /** 注销后的封口禁止新请求进入业务 handler。 */
    seal() {
      if (sealed) return;
      sealed = true;
      for (const hook of [...sealHooks]) {
        try { hook(); } catch { /* 同步释放失败由后续 quiescence/Context close 兜底。 */ }
      }
      sealHooks.clear();
    },
  };
}

/**
 * 把 Route callback 包装为不向 Playwright EventEmitter 返回 rejected Promise 的安全处理器。
 * 底层异常不进入 supervisor；best-effort abort 失败也只触发固定非通过，不会泄漏第二条栈。
 */
function superviseBrowserRoute(supervisor, stage, handler, admissionGuard = null) {
  assert.equal(typeof supervisor?.fail, "function", "缺少 Route 异常监督器");
  assert.equal(typeof handler, "function", "Route handler 非函数");
  if (admissionGuard) {
    assert.equal(typeof admissionGuard.allowsBusinessHandler, "function", "Route admission 门禁非法");
  }
  return async (rawRoute) => {
    const ticket = supervisor.enter();
    if (!ticket.allowed) {
      try {
        // 角色已封口时只中断关闭期请求，不能再触发业务 handler 或记录 shutdown 假阳性。
        await rawRoute.abort("failed");
      } catch {
        // Context 可能已经关闭；关闭期底层错误不得离开 callback。
      } finally {
        ticket.finish();
      }
      return;
    }
    if (admissionGuard && !admissionGuard.allowsBusinessHandler()) {
      // gate-local dispose 已承诺立即调用 page.unroute；此处不得再执行业务 handler 或 terminal，
      // 否则 Playwright 的卸载自动推进可能与 fallback/abort 形成对同一 Route 的二次处理。
      ticket.finish();
      return;
    }
    let terminalStarted = false;
    let handlerFailed = false;
    // facade 在调用真实 terminal 方法前置位，catch 便不会对同一 Route 再执行 abort。
    const terminal = (name) => (...args) => {
      terminalStarted = true;
      return rawRoute[name](...args);
    };
    const route = {
      request: () => rawRoute.request(),
      fetch: (...args) => rawRoute.fetch(...args),
      fallback: terminal("fallback"),
      continue: terminal("continue"),
      abort: terminal("abort"),
      fulfill: terminal("fulfill"),
    };
    try {
      await handler(route);
    } catch {
      handlerFailed = true;
      if (!terminalStarted) {
        try {
          terminalStarted = true;
          await rawRoute.abort("failed");
        } catch {
          // 首次失败已安全登记；底层 abort 异常或 URL 不得再次离开 callback。
        }
      }
    } finally {
      ticket.finish();
      // 必须等 best-effort terminal 与 active 记账都完成后再发布首错，gate 才不会抢先 unroute。
      if (handlerFailed) supervisor.fail(stage);
    }
  };
}

/**
 * 创建单个 page.route handler 的接纳门禁；关闭只影响当前 gate，不封口角色级 Context guard。
 * stopAccepting 是同步幂等操作，保证 dispose 可在同一 JS turn 内先阻止新业务 callback，再卸载路由。
 */
function createBrowserGateAdmissionGuard() {
  let accepting = true;
  return Object.freeze({
    /** 返回当前 handler 是否仍可进入会产生网络/状态副作用的业务逻辑。 */
    allowsBusinessHandler: () => accepting,
    /** 永久关闭当前 gate 的业务接纳；迟到 callback 留给紧随其后的 page.unroute 推进。 */
    stopAccepting() { accepting = false; },
  });
}

/** page route 非目标请求必须 fallback 给 Context 门禁；continue 会绕过后续 handler。 */
async function fallThroughBrowserRoute(route) {
  assert.equal(typeof route?.fallback, "function", "Playwright route 缺少 fallback");
  await route.fallback();
}

/**
 * 有界执行一个 Playwright Route 操作，并把底层 URL/正文/stack 压缩成固定 stage/code。
 * Promise 的成功与失败都先映射为只 resolve 的 outcome，确保 timeout 在 finally 中必定清除。
 */
async function runBoundedBrowserRouteOperation(
  action,
  stage,
  timeoutMillis = 30_000,
) {
  assert.equal(typeof action, "function", "Route 有界操作非函数");
  assert.ok(SAFE_STAGE_PATTERN.test(String(stage)), "Route 有界操作 stage 非安全值");
  assert.ok(Number.isInteger(timeoutMillis) && timeoutMillis >= 1 && timeoutMillis <= 30_000);
  let timeout;
  try {
    const outcome = await Promise.race([
      Promise.resolve().then(action).then(
        (value) => ({ kind: "fulfilled", value }),
        () => ({ kind: "rejected" }),
      ),
      new Promise((resolve) => {
        timeout = setTimeout(() => resolve({ kind: "timeout" }), timeoutMillis);
      }),
    ]);
    if (outcome.kind === "timeout") {
      // 超时只公开固定 stage/code，底层仍在途 Promise 已由 fulfilled/rejected outcome 分支观察。
      throw new BrowserAcceptanceStageError(stage, "timeout");
    }
    if (outcome.kind === "rejected") {
      // Playwright 原错可能含 URL 或调用栈，必须在 Route 边界直接压缩而不是向上透传。
      throw new BrowserAcceptanceStageError(stage, "operation_failed");
    }
    return outcome.value;
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * 等待命中 Request 的真实网络终态后再移除 page route。
 *
 * route.fallback 只把控制权交给下一个 handler，不等待 Context guard 的 continue/abort。
 * 若此时立即 unroute，Playwright 会自动推进仍在途的 Route，随后 Context 再处理便会双放行。
 */
async function settleBrowserRouteBeforeUnroute(
  request,
  handlerSettled,
  unroute,
  timeoutMillis = 30_000,
) {
  assert.equal(typeof request?.response, "function", "缺少命中 Request");
  assert.ok(handlerSettled && typeof handlerSettled.then === "function", "缺少 handler 终态确认");
  assert.equal(typeof unroute, "function", "缺少 Route 卸载函数");
  assert.ok(Number.isInteger(timeoutMillis) && timeoutMillis >= 1 && timeoutMillis <= 30_000);
  const status = await runBoundedBrowserRouteOperation(async () => {
    // page callback 必须先完成自身 fallback/abort/fulfill，null 响应也不能冒充 terminal acknowledgement。
    await handlerSettled;
    const response = await request.response();
    return response ? response.status() : null;
  }, "browser.route.settlement", timeoutMillis);
  // 只有 terminal 与 Request 已收敛才允许卸载；失败/超时会保留 handler 交给 Context close。
  await runBoundedBrowserRouteOperation(unroute, "browser.route.unroute", timeoutMillis);
  return status;
}

/**
 * 创建幂等 gate 卸载器；异常路径也先释放 handler，再根据是否命中决定等待 Request 或直接卸载。
 * matchedRequest 使用 getter 读取最新值，避免 gate 安装时把 null 固化进闭包。
 */
function createBrowserGateDisposer({
  matchedRequest,
  handlerSettled,
  release,
  unroute,
  admissionGuard,
}) {
  assert.equal(typeof matchedRequest, "function", "缺少 gate Request getter");
  assert.equal(typeof release, "function", "缺少 gate 释放函数");
  assert.equal(typeof unroute, "function", "缺少 gate 卸载函数");
  assert.equal(typeof admissionGuard?.stopAccepting, "function", "缺少 gate-local admission 门禁");
  let disposal = null;
  return () => {
    // 必须先同步停收再释放旧 callback；这样 pre-drain 后可在无接纳窗口内原子启动 page.unroute。
    admissionGuard.stopAccepting();
    release();
    if (!disposal) {
      disposal = (async () => {
        const request = matchedRequest();
        if (!request) {
          // 未命中也必须有界卸载，避免 action-before-request 失败后卡死在 page.unroute。
          await runBoundedBrowserRouteOperation(unroute, "browser.route.unroute");
          return null;
        }
        return settleBrowserRouteBeforeUnroute(request, handlerSettled, unroute);
      })();
    }
    return disposal;
  };
}

/** route.fetch 前复用全局网络/URL 隐私门禁，并拒绝错误 method/path 或任何目标 PATCH query。 */
function assertGuardedRouteFetchRequest(request, state) {
  const raw = {
    url: request.url(),
    method: request.method(),
    headers: request.headers(),
  };
  const decision = decideBrowserNetworkRequest(raw, state.origins);
  assert.deepEqual(decision, { allowed: true, destination: "control", violation: null });
  assertUrlPrivacy(raw.url, {
    allowedOrigins: Object.values(state.origins),
    exactSecrets: state.secretRegistry,
    forbiddenValues: state.forbiddenUrlValues,
  });
  const target = new URL(raw.url);
  assert.equal(raw.method, "PATCH", "受控 route.fetch 只允许 PATCH");
  assert.equal(
    target.pathname,
    `/model-aliases/${state.runtime.aliasId}/cache-affinity`,
    "受控 route.fetch 路径不匹配",
  );
  assert.equal(target.search, "", "受控 PATCH 不允许 query");
  return true;
}

/** 校验原始响应头数组中指定名称和值恰好出现一次；只返回布尔事实，不保留 Header 正文。 */
function hasSingleExactResponseHeader(responseHeaderEntries, name, expectedValue) {
  if (!Array.isArray(responseHeaderEntries)) return false;
  const values = responseHeaderEntries
    .filter((item) => String(item?.name ?? "").toLowerCase() === name.toLowerCase())
    .map((item) => String(item?.value ?? ""));
  return values.length === 1 && values[0] === expectedValue;
}

/**
 * 校验业务响应只返回一条精确 ACAO；重复同值也拒绝，避免扁平 Header API 覆盖重复行后假通过。
 * OPTIONS 的 ACAO 由预检完整合同统一检查，因此这里返回 null 保持两类证据互斥。
 */
function targetCorsOriginValid({ method, responseHeaderEntries, consoleOrigin }) {
  if (String(method).toUpperCase() === "OPTIONS") return null;
  return hasSingleExactResponseHeader(
    responseHeaderEntries,
    "access-control-allow-origin",
    consoleOrigin,
  );
}

/**
 * 校验非预检目标请求确实由 Console Origin 发起；OPTIONS 的同一事实由完整预检合同验证。
 * 返回值只保存布尔/null，不把请求 Header 原文写入稳定证据。
 */
function targetRequestCorsOriginValid({ method, requestHeaders, consoleOrigin }) {
  if (String(method).toUpperCase() === "OPTIONS") return null;
  return normalizedHeaders(requestHeaders).origin === consoleOrigin;
}

/** 校验 OPTIONS 状态、Origin、允许方法/请求头、no-store 与唯一精确 ACAO 的完整预检合同。 */
function targetPreflightHeadersValid({
  method,
  status,
  responseHeaders,
  responseHeaderEntries,
  requestHeaders,
  consoleOrigin,
}) {
  if (String(method).toUpperCase() !== "OPTIONS") return null;
  const requestedMethod = String(requestHeaders["access-control-request-method"] ?? "")
    .toUpperCase();
  const requestedHeaders = new Set(
    String(requestHeaders["access-control-request-headers"] ?? "")
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean),
  );
  const allowedMethods = new Set(
    String(responseHeaders["access-control-allow-methods"] ?? "")
      .split(",")
      .map((item) => item.trim().toUpperCase())
      .filter(Boolean),
  );
  const allowedHeaders = new Set(
    String(responseHeaders["access-control-allow-headers"] ?? "")
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean),
  );
  return status === 200
    && requestHeaders.origin === consoleOrigin
    && hasSingleExactResponseHeader(
      responseHeaderEntries,
      "access-control-allow-origin",
      consoleOrigin,
    )
    && requestedMethod !== ""
    && allowedMethods.has(requestedMethod)
    && [...requestedHeaders].every((item) => allowedHeaders.has(item))
    && String(responseHeaders["cache-control"] ?? "").startsWith("no-store");
}

/** 只投影预检有效性或冲突 GET 版本，正文、分类和 Header 原值不进入 evidence。 */
function projectTargetResponseBody(text, surface, state, status, method = "GET") {
  if (String(method).toUpperCase() === "OPTIONS") {
    assert.equal(text, "OK", "目标 API 预检正文必须是固定 OK");
    return { bodyJsonValid: null, preflightBodyValid: true, cacheAffinityVersion: null };
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    assert.fail("目标 API 响应不是合法 JSON");
  }
  const projection = {
    bodyJsonValid: true, preflightBodyValid: null, cacheAffinityVersion: null,
  };
  // ModelAlias GET 只投影目标配置版本，用于证明 409 后 GET；分类和完整正文立即丢弃。
  if (surface === "model_alias_list" && status === 200) {
    const alias = (body.model_aliases ?? []).find(
      (item) => item?.tenant_id === state.runtime.tenantId
        && item?.model_alias_id === state.runtime.aliasId,
    );
    if (alias?.cache_affinity) projection.cacheAffinityVersion = Number(alias.cache_affinity.version);
  }
  return projection;
}

/**
 * 异步扫描每一条 PAGE-072/073 目标 API 的原始 Header 与正文。
 * 任一 ACAO、预检、JSON 或隐私失败都只记录固定 contract 类别，不保存正文、Header 或底层异常。
 */
function scheduleTargetResponseScan(response, record, state, evidence) {
  const task = (async () => {
    try {
      // headersArray 保留重复响应头；与正文一起纳入已跟踪任务，EventEmitter 不承接异步异常。
      const [text, responseHeaderEntries] = await Promise.all([
        response.text(),
        response.headersArray(),
      ]);
      assert.equal(containsSecret(text, state.secretRegistry), false, "目标 API 响应含敏感值");
      const responseHeaders = response.headers();
      const requestHeaders = response.request().headers();
      record.corsOriginValid = targetCorsOriginValid({
        method: record.method,
        responseHeaderEntries,
        consoleOrigin: state.origins.consoleOrigin,
      });
      record.preflightHeadersValid = targetPreflightHeadersValid({
        method: record.method,
        status: record.status,
        responseHeaders,
        responseHeaderEntries,
        requestHeaders,
        consoleOrigin: state.origins.consoleOrigin,
      });
      if (record.method === "OPTIONS") {
        assert.equal(record.preflightHeadersValid, true, "目标 API 预检响应头不完整");
      } else {
        assert.equal(record.corsOriginValid, true, "目标 API 业务响应 ACAO 不唯一或未精确回显");
      }
      Object.assign(record, projectTargetResponseBody(
        text, record.surface, state, record.status, record.method,
      ));
      record.bodyPrivacyScanned = true;
      evidence.bodyPrivacyScanCount += 1;
    } catch {
      record.bodyPrivacyScanned = false;
      record.bodyJsonValid = record.method === "OPTIONS" ? null : false;
      record.preflightBodyValid = record.method === "OPTIONS" ? false : null;
      record.preflightHeadersValid = record.method === "OPTIONS" ? false : null;
      record.corsOriginValid = record.method === "OPTIONS" ? null : false;
      evidence.violations.push("target_response_contract");
    }
  })();
  evidence.pendingResponseScans.add(task);
  void task.finally(() => evidence.pendingResponseScans.delete(task));
  return task;
}

/** 等待当前已登记的目标响应扫描收敛；循环处理等待期间刚登记的新响应。 */
async function settleTargetResponseScans(evidence) {
  while (evidence.pendingResponseScans.size) {
    await Promise.all([...evidence.pendingResponseScans]);
  }
  return evidence.bodyPrivacyScanCount;
}

/** 为一次受控失败响应或故意中断创建有界 Console transport 窗口，窗口关闭后绝不再吞后续错误。 */
function beginExpectedTransportConsoleError(evidence, surface) {
  const window = {
    id: `transport_${evidence.expectedTransportErrorCount + 1}`,
    surface,
    active: true,
    observed: 0,
  };
  evidence.transportErrorWindows.push(window);
  evidence.expectedTransportErrorCount += 1;
  return window;
}

/**
 * 把未获豁免的 Console error 压成固定类别；分类只在当前调用栈读取正文，不保留原文或 location。
 * 这些模式覆盖 Chrome 常见网络、CORS、表单、脚本与安全策略诊断，未知类型继续失败关闭。
 */
function classifyUnexpectedBrowserConsoleError(text) {
  const value = String(text ?? "");
  // CORS 文案可能同时包含 “Failed to load resource” 或 net::ERR_FAILED；必须优先归入
  // 独立安全类别，避免被受控 transport 窗口消费。
  if (/(?:CORS|cross-origin|Access to fetch)/i.test(value)) return "console_cors_error";
  // Chrome transport 文案只归类，不与业务 error code 或 HTTP 状态混用。
  if (EXPECTED_TRANSPORT_CONSOLE_ERROR.test(value)) return "console_transport_error";
  // 浏览器原生表单可聚焦性错误属于页面交互缺陷，需要单独失败关闭。
  if (/(?:invalid form control|not focusable)/i.test(value)) return "console_form_error";
  // 异常构造名足以定位错误类别，原始异常文本和 stack 仍立即丢弃。
  if (/TypeError/i.test(value)) return "console_type_error";
  if (/ReferenceError/i.test(value)) return "console_reference_error";
  if (/SyntaxError/i.test(value)) return "console_syntax_error";
  // RangeError 常见于递归或边界失控，必须与普通脚本错误分开，便于在不暴露 stack 的前提下定位页面缺陷。
  if (/RangeError/i.test(value)) return "console_range_error";
  // 仅保留“未捕获”这一生命周期事实，异常正文可能含请求参数、Prompt 或内部标识，不能写入验收制品。
  if (/Uncaught/i.test(value)) return "console_uncaught_error";
  // 无法识别具体构造名时只记录通用 Error 类别，继续失败关闭且不保存浏览器输出原文。
  if (/\bError:/i.test(value)) return "console_error_object";
  if (/(?:Content Security Policy|Refused to|blocked by client)/i.test(value)) {
    return "console_policy_error";
  }
  return "console_other_error";
}

/** 仅匹配活动窗口、精确目标 API location 和 Chrome 固定网络错误；任意其他 Console error 失败关闭。 */
function recordBrowserConsoleError(evidence, event, origins) {
  const surface = classifyTargetApi(event.locationUrl, origins.controlOrigin);
  const category = classifyUnexpectedBrowserConsoleError(event.text);
  const window = evidence.transportErrorWindows.find(
    (item) => item.active && item.observed === 0 && item.surface === surface,
  );
  // 只有活动窗口、精确接口 location 和严格 transport 类别三者同时匹配才允许忽略一次；
  // CORS 与 transport 组合文案已优先归入 CORS，绝不能消耗窗口配额。
  if (window && category === "console_transport_error") {
    window.observed = 1;
    evidence.observedTransportErrorCount += 1;
    return { expected: true };
  }
  // 未处于授权窗口的 Chrome 网络错误与普通 Console error 分开计类，但都不保存正文或 URL。
  evidence.browserErrors.push(category);
  return { expected: false };
}

/** 关闭 transport 窗口并推进 settled 计数；允许 Chrome 不产生日志，但最多接纳一条精确匹配。 */
function settleExpectedTransportConsoleError(evidence, window) {
  assert.ok(window && window.active && [0, 1].includes(window.observed));
  window.active = false;
  evidence.settledTransportErrorCount += 1;
  return { observed: window.observed, settled: true };
}

/** 断言所有 transport 窗口均已关闭，避免未消费配额吞掉后续未知 Console error。 */
function assertTransportConsoleConverged(evidence) {
  assert.equal(evidence.settledTransportErrorCount, evidence.expectedTransportErrorCount);
  assert.ok(evidence.transportErrorWindows.every((item) => !item.active));
  assert.ok(evidence.observedTransportErrorCount <= evidence.expectedTransportErrorCount);
  return {
    expected: evidence.expectedTransportErrorCount,
    observed: evidence.observedTransportErrorCount,
    settled: evidence.settledTransportErrorCount,
  };
}

/**
 * 收敛并复核不依赖活页面 DOM 的浏览器守卫证据。
 *
 * 该函数既供每个页面 checkpoint 使用，也供 Context 关闭后的最终门禁复用。最终门禁不能再读取
 * 已关闭页面，但仍必须消费目标响应扫描，并拒绝迟到的网络越界、Console/PageError 和未关闭豁免窗口。
 */
async function assertBrowserGuardEvidence(evidence) {
  await runAcceptanceStage(
    "browser.scan.response_privacy",
    () => settleTargetResponseScans(evidence),
  );
  await runAcceptanceStage("browser.scan.blocked_request", async () => {
    assert.equal(evidence.blockedRequestCount, 0);
  });
  await runAcceptanceStage("browser.scan.violation", async () => {
    assert.deepEqual(evidence.violations, []);
  });
  await runAcceptanceStage("browser.scan.browser_error", async () => {
    const allowedKinds = new Set([
      "http_5xx", "pageerror", "console_transport_error", "console_cors_error",
      "console_form_error", "console_type_error", "console_reference_error", "console_syntax_error",
      "console_range_error", "console_uncaught_error", "console_error_object",
      "console_policy_error", "console_other_error",
    ]);
    const firstKind = evidence.browserErrors.find((kind) => allowedKinds.has(kind)) ?? "unknown";
    // 只公开首个闭集类别；数组内容、Console 正文和页面 URL 均不得离开 evidence 内存。
    if (evidence.browserErrors.length) {
      throw new BrowserAcceptanceStageError(
        `browser.scan.browser_error.${firstKind}`,
        "observed",
      );
    }
  });
  await runAcceptanceStage("browser.scan.transport", async () => {
    assertTransportConsoleConverged(evidence);
  });
  return true;
}

/** 有界轮询 Node 侧 evidence，避免用固定 sleep 猜测 409 后 GET 时序。 */
async function waitForEvidence(predicate, timeoutMillis = 20_000) {
  const deadline = Date.now() + timeoutMillis;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  // 超时只返回固定中文错误，不把等待中的 URL、ID 或响应正文带出 evidence 边界。
  throw new Error("浏览器证据等待超时");
}

/** 证明 409 响应之后出现真实同租户 ModelAlias GET，并读到外部推进后的精确版本。 */
function assertConflictRefreshEvidence(records, expectedVersion) {
  const conflictIndex = records.findIndex(
    (item) => item.surface === "cache_config_write" && item.method === "PATCH" && item.status === 409,
  );
  assert.ok(conflictIndex >= 0, "缺少 CacheAffinity 409 响应证据");
  const refresh = records.slice(conflictIndex + 1).find(
    (item) => item.surface === "model_alias_list" && item.method === "GET" && item.status === 200,
  );
  assert.ok(refresh, "409 后缺少 ModelAlias GET");
  assert.ok(refresh.requestIdPresent && refresh.traceIdPresent && refresh.noStore);
  assert.ok(refresh.bodyPrivacyScanned && refresh.bodyJsonValid);
  assert.equal(refresh.cacheAffinityVersion, expectedVersion);
  return { conflictIndex, refreshResponseIndex: refresh.responseIndex, version: expectedVersion };
}

/** 在 Playwright Context 上安装精确 origin、目标 API 元数据和浏览器异常门禁。 */
async function installBrowserGuards(context, page, state, role) {
  const routeSupervisor = createBrowserRouteFailureSupervisor();
  const evidence = {
    role,
    allowedOrigins: Object.values(state.origins),
    forbiddenUrlValues: state.forbiddenUrlValues,
    targetRequests: [],
    targetResponses: [],
    violations: [],
    browserErrors: [],
    blockedRequestCount: 0,
    checkpointCount: 0,
    expectedTransportErrorCount: 0,
    observedTransportErrorCount: 0,
    settledTransportErrorCount: 0,
    transportErrorWindows: [],
    pendingResponseScans: new Set(),
    bodyPrivacyScanCount: 0,
    explicitBodyReadCount: 0,
    routeSupervisor,
  };
  // Request 对象仅能在当前页面生命周期内作为 WeakMap 键存在；稳定 evidence 只保留递增索引，
  // 不暴露 URL、请求头或 Playwright 内部标识。
  const targetRequestIndexes = new WeakMap();
  const targetRequestRecords = new Map();
  const respondedTargetRequestIndexes = new Set();
  const contextRouteHandler = superviseBrowserRoute(
    routeSupervisor,
    "browser.route." + role + ".context",
    async (route) => {
      const request = route.request();
      const decision = decideBrowserNetworkRequest({
        url: request.url(),
        method: request.method(),
        headers: request.headers(),
      }, state.origins);
      if (!decision.allowed) {
        evidence.blockedRequestCount += 1;
        evidence.violations.push(decision.violation);
        await route.abort("blockedbyclient");
        return;
      }
      if (decision.destination !== "browser_internal") {
        try {
          assertUrlPrivacy(request.url(), {
            allowedOrigins: Object.values(state.origins),
            exactSecrets: state.secretRegistry,
            forbiddenValues: state.forbiddenUrlValues,
          });
        } catch {
          evidence.blockedRequestCount += 1;
          evidence.violations.push("url_privacy");
          await route.abort("blockedbyclient");
          return;
        }
      }
      await route.continue();
    },
  );
  await context.route("**/*", contextRouteHandler);
  page.on("request", (request) => {
    const surface = classifyTargetApi(request.url(), state.origins.controlOrigin);
    if (!surface) return;
    if (targetRequestIndexes.has(request)) {
      evidence.violations.push("target_request_duplicate");
      return;
    }
    const requestIndex = evidence.targetRequests.length;
    const method = request.method();
    const record = {
      requestIndex,
      method,
      surface,
      requestCorsOriginValid: targetRequestCorsOriginValid({
        method,
        requestHeaders: request.headers(),
        consoleOrigin: state.origins.consoleOrigin,
      }),
    };
    targetRequestIndexes.set(request, requestIndex);
    targetRequestRecords.set(requestIndex, record);
    evidence.targetRequests.push(record);
  });
  page.on("response", (response) => {
    const surface = classifyTargetApi(response.url(), state.origins.controlOrigin);
    if (surface) {
      const headers = response.headers();
      const targetRequest = response.request();
      const method = targetRequest.method();
      const requestIndex = targetRequestIndexes.get(targetRequest);
      const pairedRequest = targetRequestRecords.get(requestIndex);
      if (!pairedRequest) {
        evidence.violations.push("target_response_unpaired");
      } else {
        if (respondedTargetRequestIndexes.has(requestIndex)) {
          evidence.violations.push("target_response_duplicate");
        }
        respondedTargetRequestIndexes.add(requestIndex);
        if (pairedRequest.method !== method || pairedRequest.surface !== surface) {
          evidence.violations.push("target_response_pair_mismatch");
        }
      }
      const record = {
        responseIndex: evidence.targetResponses.length,
        requestIndex: Number.isInteger(requestIndex) ? requestIndex : null,
        method,
        surface,
        status: response.status(),
        requestIdPresent: Boolean(headers["x-request-id"]),
        traceIdPresent: Boolean(headers["x-trace-id"]),
        noStore: String(headers["cache-control"] ?? "").startsWith("no-store"),
        // 原始 ACAO 数组与完整预检由 pendingResponseScans 异步填充；初值不能代表通过。
        corsOriginValid: null,
        bodyPrivacyScanned: false,
        bodyJsonValid: false,
        preflightBodyValid: null,
        preflightHeadersValid: null,
        cacheAffinityVersion: null,
      };
      evidence.targetResponses.push(record);
      scheduleTargetResponseScan(response, record, state, evidence);
    }
    // 任意目标或非目标 5xx 都属于未知浏览器错误，不能由后续成功请求抵消。
    if (response.status() >= 500) evidence.browserErrors.push("http_5xx");
  });
  page.on("pageerror", () => evidence.browserErrors.push("pageerror"));
  page.on("console", (message) => {
    // 非 error 级 Console 信息不参与失败门禁，也不保存其正文。
    if (message.type() !== "error") return;
    recordBrowserConsoleError(evidence, {
      text: message.text(),
      locationUrl: message.location()?.url ?? "",
    }, state.origins);
  });
  return evidence;
}

/** 读取当前 DOM/URL/Storage；原值只在当前断言栈驻留。 */
async function readSensitiveSurfaceSnapshot(page) {
  return page.evaluate(() => ({
    domText: document.documentElement?.innerText ?? "",
    domHtml: document.documentElement?.outerHTML ?? "",
    url: window.location.href,
    localStorageEntries: Object.entries(window.localStorage),
    sessionStorageEntries: Object.entries(window.sessionStorage),
  }));
}

/** 拒绝 PAGE-072/073 运行时内部字段名进入业务 DOM；中文“绑定”仅是产品状态标签。 */
async function assertCacheDomProjection(page) {
  const fragments = await page.locator(
    ".cache-affinity-config-row, .cache-affinity-selection-section",
  ).evaluateAll((elements) => elements.map((element) => element.outerHTML));
  const text = fragments.join("\n");
  assert.doesNotMatch(
    text,
    /(?:provider_credential|credential_id|binding_id|scope_id|key_prefix|data_class|prompt_fingerprint|endpoint_revision_digest|affinity_key_digest)/i,
  );
  return true;
}

/** 在截图和状态转换前后联合检查网络、DOM、URL、Storage 与内部字段投影。 */
async function scanBrowserCheckpoint(
  page,
  evidence,
  exactSecrets,
  { activeToken = null, expectedOwner = null, loggedOut = false, label = "browser" } = {},
) {
  await assertBrowserGuardEvidence(evidence);
  const privacy = await runAcceptanceStage("browser.scan.sensitive_surface", async () => (
    assertSensitiveSurfaceSnapshot(await readSensitiveSurfaceSnapshot(page), {
      exactSecrets,
      forbiddenUrlValues: stateForbiddenUrlValues(evidence),
      allowedOrigins: evidence.allowedOrigins,
      activeToken,
      expectedOwner,
      loggedOut,
      label,
    })
  ));
  await runAcceptanceStage("browser.scan.dom_projection", () => assertCacheDomProjection(page));
  // 前置门禁之后仍有多个异步 DOM/Storage 读取；末尾复核可捕获该窗口内迟到的 Console/PageError。
  await assertBrowserGuardEvidence(evidence);
  evidence.checkpointCount += 1;
  return privacy;
}

/** 从 evidence 读取只驻内存的 URL 受限值；结果对象永不序列化该数组。 */
function stateForbiddenUrlValues(evidence) {
  return Array.isArray(evidence.forbiddenUrlValues) ? evidence.forbiddenUrlValues : [];
}

/** 显式读取目标响应供业务断言；完整隐私计数由全局 response listener 独立维护。 */
async function scanTargetResponse(response, state, evidence) {
  const text = await response.text();
  assert.equal(containsSecret(text, state.secretRegistry), false);
  evidence.explicitBodyReadCount += 1;
  return JSON.parse(text);
}

/** 生成不含 tenant、Alias、分类或凭据的有界 request_id。 */
function operationRequestId(kind, suffix) {
  return `req_prd050_browser_${kind}_${suffix}`.slice(0, 128);
}

/** 安全解析 fetch JSON；失败返回空对象，调用方只按状态和白名单字段断言。 */
async function readJson(response) {
  try {
    const body = await response.json();
    return body && typeof body === "object" && !Array.isArray(body) ? body : {};
  } catch {
    return {};
  }
}

/** 发送带硬超时的真实 Control API 请求，并核对统一 request_id/trace_id。 */
async function controlJson(state, pathname, options, expectedStatus, expectedCode = null) {
  const response = await fetch(new URL(pathname, state.origins.controlOrigin), {
    ...options,
    signal: options.signal ?? AbortSignal.timeout(20_000),
  });
  const body = await readJson(response);
  assert.equal(response.status, expectedStatus);
  assert.ok(body.request_id && body.trace_id);
  assert.equal(body.request_id, response.headers.get("X-Request-ID"));
  assert.equal(body.trace_id, response.headers.get("X-Trace-ID"));
  if (expectedCode !== null) assert.equal(body.code, expectedCode);
  return { body, response };
}

/** 构造 Bearer 与链路头；tenant_id 永远不由浏览器脚本提交。 */
function authHeaders(token, requestId) {
  return { Authorization: `Bearer ${token}`, "X-Request-ID": requestId };
}

/** 在任何可能提交的写请求前登记安全意图，正文和敏感配置值不得进入账本。 */
function registerMutation(state, kind) {
  const item = { kind, intentId: `mut_${++state.mutationSequence}`, requestId: "", outcome: "pending" };
  state.mutationLedger.push(item);
  return item;
}

/** 把浏览器生成的 request_id 和固定结果绑定到已登记意图。 */
function settleMutation(item, { requestId = "", outcome }) {
  assert.ok(item && item.outcome === "pending");
  if (requestId) assert.ok(REQUEST_ID_PATTERN.test(String(requestId)));
  assert.ok(["committed", "rejected", "aborted"].includes(outcome));
  item.requestId = String(requestId);
  item.outcome = outcome;
}

/** 登记 Session owner 并把 Token 加入进程内扫描集合；结果永不序列化这些值。 */
function registerSession(state, body, expectedOwner) {
  const sessionId = requiredResourceId(body.session_id, "sessionId");
  assert.ok(
    body.user?.tenant_id === expectedOwner.tenantId
      && body.user?.user_id === expectedOwner.userId
      && body.user?.email === expectedOwner.email
      && body.user?.role === expectedOwner.role,
  );
  state.sessionOwners.set(sessionId, { ...expectedOwner });
  const token = String(body.access_token ?? "");
  assert.ok(token.length >= 16);
  state.secretRegistry.add(token);
  // 返回值携带可信 owner 供每次 Storage 快照复核，Token 仍只驻当前进程内存。
  return { token, sessionId, owner: { ...expectedOwner, sessionId } };
}

/** 通过真实登录 API 建立 Session，并在请求前登记 User-Agent owner intent。 */
async function apiLogin(state, account, role, userId, marker) {
  const mutation = registerMutation(state, `login_${role}`);
  const requestId = operationRequestId(`api_login_${role}`, state.browserSuffix);
  state.sessionIntents.set(marker, {
    tenantId: state.runtime.tenantId,
    userId,
    email: account.email,
    role,
  });
  const { body } = await controlJson(state, "/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Request-ID": requestId, "User-Agent": marker },
    body: JSON.stringify({
      tenant_slug: state.args.tenantSlug,
      email: account.email,
      password: account.password,
    }),
  }, 200);
  settleMutation(mutation, { requestId, outcome: "committed" });
  // 登录响应先登记 Session owner，再把最小会话投影交给调用方。
  return registerSession(state, body, {
    tenantId: state.runtime.tenantId,
    userId,
    email: account.email,
    role,
  });
}

/** 使用真实管理员 API 创建临时 developer/viewer，并为后端 RBAC 探针登录。 */
async function prepareAccounts(state) {
  const adminSession = await apiLogin(
    state,
    state.accounts.admin,
    "admin",
    state.facts.adminUserId,
    `prd050-browser-bootstrap-${state.browserSuffix}`,
  );
  state.apiTokens.set("admin", adminSession.token);
  for (const role of ["developer", "viewer"]) {
    const account = {
      email: `prd050-browser-${role}-${state.browserSuffix}@metahub.local`,
      displayName: `PRD050 browser ${role} ${state.browserSuffix}`,
      password: `Prd050Browser!${randomBytes(18).toString("base64url")}`,
      role,
    };
    state.secretRegistry.add(account.password);
    state.userIntents.set(role, account);
    const mutation = registerMutation(state, `create_${role}`);
    const requestId = operationRequestId(`create_${role}`, state.browserSuffix);
    const { body } = await controlJson(state, "/users", {
      method: "POST",
      headers: { ...authHeaders(adminSession.token, requestId), "Content-Type": "application/json" },
      body: JSON.stringify({
        email: account.email,
        display_name: account.displayName,
        password: account.password,
        role,
      }),
    }, 201);
    const user = body.user ?? {};
    const userId = requiredResourceId(user.user_id, `${role}UserId`);
    assert.ok(
      user.tenant_id === state.runtime.tenantId
        && user.email === account.email
        && user.display_name === account.displayName
        && user.role === role
        && user.status === "active",
    );
    settleMutation(mutation, { requestId, outcome: "committed" });
    state.temporaryUserOwners.set(userId, {
      tenantId: state.runtime.tenantId,
      email: account.email,
      displayName: account.displayName,
      role,
    });
    state.accounts[role] = { ...account, userId };
    const roleSession = await apiLogin(
      state,
      account,
      role,
      userId,
      `prd050-browser-api-${role}-${state.browserSuffix}`,
    );
    state.apiTokens.set(role, roleSession.token);
  }
  const emptyAliasName = `prd050-browser-empty-${state.browserSuffix}`;
  state.emptyAliasIntent = {
    tenantId: state.runtime.tenantId,
    alias: emptyAliasName,
    environmentId: state.facts.environmentId,
    modelId: state.runtime.modelId,
    createdBy: state.facts.adminUserId,
  };
  const aliasMutation = registerMutation(state, "create_empty_alias");
  const aliasRequestId = operationRequestId("create_empty_alias", state.browserSuffix);
  const { body: aliasBody } = await controlJson(state, "/model-aliases", {
    method: "POST",
    headers: { ...authHeaders(adminSession.token, aliasRequestId), "Content-Type": "application/json" },
    body: JSON.stringify({
      alias: emptyAliasName,
      environment_id: state.facts.environmentId,
      default_model_id: state.runtime.modelId,
      fallback_model_id: null,
      description: "PRD-050 browser empty-state acceptance",
      status: "disabled",
    }),
  }, 201);
  const emptyAlias = aliasBody.model_alias ?? {};
  const emptyAliasId = requiredResourceId(emptyAlias.model_alias_id, "emptyAliasId");
  assert.ok(
    emptyAlias.tenant_id === state.runtime.tenantId
      && emptyAlias.alias === emptyAliasName
      && emptyAlias.environment_id === state.facts.environmentId
      && emptyAlias.default_model_id === state.runtime.modelId
      && emptyAlias.status === "disabled"
      && emptyAlias.cache_affinity == null,
  );
  settleMutation(aliasMutation, { requestId: aliasRequestId, outcome: "committed" });
  state.emptyAliasOwner = {
    modelAliasId: emptyAliasId,
    tenantId: state.runtime.tenantId,
    alias: emptyAliasName,
    createdBy: state.facts.adminUserId,
  };
  // 空态 Alias 创建即为 disabled 终态，cleanup 仅复验 owner，不合成或删除业务事实。
  return { temporaryAccountCount: 2, apiSessionCount: 3, terminalEmptyAliasCount: 1 };
}

/** 从 ModelAlias 列表读取当前租户目标配置；只返回内存中的配置白名单。 */
async function readCurrentConfig(state, token, label) {
  const requestId = operationRequestId(`config_get_${label}`, state.browserSuffix);
  const { body } = await controlJson(state, "/model-aliases", {
    headers: authHeaders(token, requestId),
  }, 200);
  assert.equal(containsSecret(JSON.stringify(body), state.secretRegistry), false);
  const alias = (body.model_aliases ?? []).find(
    (item) => item?.tenant_id === state.runtime.tenantId
      && item?.model_alias_id === state.runtime.aliasId,
  );
  assert.ok(alias && alias.cache_affinity);
  const value = alias.cache_affinity;
  assert.equal(value.tenant_id, state.runtime.tenantId);
  assert.equal(value.model_alias_id, state.runtime.aliasId);
  assert.ok(Number.isSafeInteger(value.version) && value.version >= 1);
  return {
    status: value.status,
    data_class: value.data_class,
    minimum_prefix_chars: value.minimum_prefix_chars,
    ttl_seconds: value.ttl_seconds,
    version: value.version,
  };
}

/** 通过真实 PATCH 执行一项预登记配置写，响应只投影为新版本与布尔状态。 */
async function patchConfigApi(state, token, desired, expectedVersion, kind) {
  const mutation = registerMutation(state, kind);
  const requestId = operationRequestId(kind, state.browserSuffix);
  const { body } = await controlJson(
    state,
    `/model-aliases/${encodeURIComponent(state.runtime.aliasId)}/cache-affinity`,
    {
      method: "PATCH",
      headers: { ...authHeaders(token, requestId), "Content-Type": "application/json" },
      body: JSON.stringify({ ...desired, expected_version: expectedVersion }),
    },
    200,
  );
  settleMutation(mutation, { requestId, outcome: "committed" });
  assert.ok(body.cache_affinity?.tenant_id === state.runtime.tenantId);
  assert.ok(body.cache_affinity?.model_alias_id === state.runtime.aliasId);
  return { changed: body.changed === true, version: Number(body.cache_affinity.version) };
}

/** 返回配置的四个业务字段；version 仅参与 CAS，不属于要恢复的语义。 */
function configSemantics(value) {
  return {
    status: value?.status,
    data_class: value?.data_class ?? null,
    minimum_prefix_chars: Number(value?.minimum_prefix_chars),
    ttl_seconds: Number(value?.ttl_seconds),
  };
}

/** 比较配置语义，不把分类值或完整对象写入错误信息。 */
function configSemanticsEqual(left, right) {
  return JSON.stringify(configSemantics(left)) === JSON.stringify(configSemantics(right));
}

/** 锁定网络结果未知重试必须复用 request_id，随后取消必须恢复最新已提交权威值。 */
function assertRetryCancelEvidence({
  abortedRequestId,
  retryRequestId,
  committedValue,
  cancelledDraftValue,
  restoredValue,
  saveDisabled,
}) {
  assert.ok(REQUEST_ID_PATTERN.test(String(abortedRequestId ?? "")));
  assert.equal(retryRequestId, abortedRequestId);
  assert.ok(Number.isSafeInteger(committedValue));
  assert.ok(Number.isSafeInteger(cancelledDraftValue));
  assert.notEqual(cancelledDraftValue, committedValue);
  assert.equal(restoredValue, committedValue);
  assert.equal(saveDisabled, true);
  return {
    sameRequestId: true,
    retryCommitted: true,
    cancelRestoredAuthority: true,
  };
}

/** 锁定 Session 切换时旧响应失效、Storage 清空和重新登录后从服务端收敛。 */
function assertSessionSwitchEvidence({
  priorSessionId,
  nextSessionId,
  logoutCompleted,
  authStorageCleared,
  lateResponseIgnored,
  committedValue,
  convergedValue,
}) {
  assert.ok(RESOURCE_ID_PATTERN.test(String(priorSessionId ?? "")));
  assert.ok(RESOURCE_ID_PATTERN.test(String(nextSessionId ?? "")));
  assert.notEqual(nextSessionId, priorSessionId);
  assert.equal(logoutCompleted, true);
  assert.equal(authStorageCleared, true);
  assert.equal(lateResponseIgnored, true);
  assert.ok(Number.isSafeInteger(committedValue));
  assert.equal(convergedValue, committedValue);
  return {
    sessionChanged: true,
    staleResponseIgnored: true,
    storageClearedBetweenSessions: true,
  };
}

/**
 * 锁定 PAGE-072 首轮读取必须来自已登录控制台的真实路由策略 loading DOM。
 * 登录表单、错误导航、缺少等待文案或提前出现配置行/编辑器都会失败关闭，不能拿其他页面冒充。
 */
function assertPage072LoadingEvidence({
  activeNavigationId,
  activeNavigationButtonCount,
  panelCount,
  titleText,
  loadingText,
  loginFormCount,
  configurationRowCount,
  editorFormCount,
}) {
  assert.equal(activeNavigationId, "routing-policies");
  assert.equal(activeNavigationButtonCount, 1);
  assert.equal(panelCount, 1);
  assert.match(String(titleText ?? ""), /^路由策略$/);
  assert.match(String(loadingText ?? ""), /等待 \/model-aliases 响应/);
  assert.equal(loginFormCount, 0);
  assert.equal(configurationRowCount, 0);
  assert.equal(editorFormCount, 0);
  return {
    activeNavigationId: "routing-policies",
    panelPresent: true,
    loginFormAbsent: true,
    configurationAbsent: true,
  };
}

/** 后端权限矩阵：reader 可读，只有 admin 可写，viewer 的畸形正文在解析前即 403。 */
const BACKEND_PERMISSION_EXPECTATIONS = Object.freeze({
  admin: Object.freeze({
    listStatus: 422, listCode: "model_alias.status_invalid",
    writeStatus: 200, writeCode: null, detailStatus: 200, detailCode: null,
  }),
  developer: Object.freeze({
    listStatus: 422, listCode: "model_alias.status_invalid",
    writeStatus: 403, writeCode: "auth.permission_denied", detailStatus: 200, detailCode: null,
  }),
  viewer: Object.freeze({
    listStatus: 403, listCode: "auth.permission_denied",
    writeStatus: 403, writeCode: "auth.permission_denied",
    detailStatus: 403, detailCode: "auth.permission_denied",
  }),
});

/** 返回后端三角色的冻结状态码/错误码合同，避免真实验收与单元测试各自漂移。 */
function backendPermissionExpectation(role) {
  const expectation = BACKEND_PERMISSION_EXPECTATIONS[String(role)];
  assert.ok(expectation, "未知 RBAC 角色");
  return expectation;
}

async function verifyBackendPermissions(state) {
  const results = {};
  for (const role of ["admin", "developer", "viewer"]) {
    const expected = backendPermissionExpectation(role);
    const token = state.apiTokens.get(role);
    const listId = operationRequestId(`rbac_list_${role}`, state.browserSuffix);
    const list = await controlJson(
      state,
      "/model-aliases?status=not-a-valid-status",
      { headers: authHeaders(token, listId) },
      expected.listStatus,
      expected.listCode,
    );
    assert.equal(containsSecret(JSON.stringify(list.body), state.secretRegistry), false);
    const writeId = operationRequestId(`rbac_write_${role}`, state.browserSuffix);
    let writeStatus = 0;
    // admin 用合法 no-op 证明可写；非 admin 用畸形正文证明权限依赖先于正文解析。
    if (role === "admin") {
      const current = await readCurrentConfig(state, token, "rbac_admin");
      const { response } = await controlJson(
        state,
        `/model-aliases/${encodeURIComponent(state.runtime.aliasId)}/cache-affinity`,
        {
          method: "PATCH",
          headers: { ...authHeaders(token, writeId), "Content-Type": "application/json" },
          body: JSON.stringify({ ...configSemantics(current), expected_version: current.version }),
        },
        expected.writeStatus,
        expected.writeCode,
      );
      writeStatus = response.status;
    } else {
      const { response } = await controlJson(
        state,
        `/model-aliases/${encodeURIComponent(state.runtime.aliasId)}/cache-affinity`,
        {
          method: "PATCH",
          headers: { ...authHeaders(token, writeId), "Content-Type": "application/json" },
          body: "not-json-and-must-not-be-read",
        },
        expected.writeStatus,
        expected.writeCode,
      );
      writeStatus = response.status;
    }
    const detailId = operationRequestId(`rbac_detail_${role}`, state.browserSuffix);
    const detail = await controlJson(
      state,
      `/request-logs/${encodeURIComponent(state.runtime.requestIds.cache_affinity_warm)}`,
      { headers: authHeaders(token, detailId) },
      expected.detailStatus,
      expected.detailCode,
    );
    assert.equal(containsSecret(JSON.stringify(detail.body), state.secretRegistry), false);
    results[role] = {
      listStatus: list.response.status,
      writeStatus,
      detailStatus: detail.response.status,
    };
  }
  // 三角色全部真实请求完成后再由闭集 helper 统一判定，避免局部状态冒充通过。
  return assertBackendPermissionEvidence(results);
}

/** 纯函数锁定后端三角色矩阵，防止前端隐藏按钮替代权限校验。 */
function assertBackendPermissionEvidence(results) {
  assert.deepEqual(results.admin, { listStatus: 422, writeStatus: 200, detailStatus: 200 });
  assert.deepEqual(results.developer, { listStatus: 422, writeStatus: 403, detailStatus: 200 });
  assert.deepEqual(results.viewer, { listStatus: 403, writeStatus: 403, detailStatus: 403 });
  return { backendRbacVerified: true, ...results };
}

/** 核对三服务健康，Gateway 健康探针不得携带任何浏览器认证凭据。 */
async function verifyServices(state) {
  const results = {};
  for (const [name, url] of [
    ["control", `${state.origins.controlOrigin}/health`],
    ["gateway", `${state.origins.gatewayOrigin}/health`],
    ["console", `${state.origins.consoleOrigin}/`],
  ]) {
    const response = await fetch(url, { signal: AbortSignal.timeout(10_000) });
    assert.equal(response.status, 200);
    results[`${name}Status`] = 200;
  }
  return results;
}

/** 使用真实 Console 登录表单，并核对 /auth/login 与 /auth/me 的同一 Session owner。 */
async function loginPage(
  page,
  state,
  role,
  requestedNavigation,
  { afterSubmit = null } = {},
) {
  const account = state.accounts[role];
  const marker = `prd050-browser-ui-${role}-${state.browserSuffix}-${++state.uiLoginSequence}`;
  state.sessionIntents.set(marker, {
    tenantId: state.runtime.tenantId,
    userId: role === "admin" ? state.facts.adminUserId : account.userId,
    email: account.email,
    role,
  });
  const mutation = registerMutation(state, `ui_login_${role}`);
  await page.goto(`${state.origins.consoleOrigin}/#${requestedNavigation}`, { waitUntil: "domcontentloaded" });
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30_000 });
  await page.locator('[name="tenantSlug"]').fill(state.args.tenantSlug);
  await page.locator('[name="email"]').fill(account.email);
  await page.locator('[name="password"]').fill(account.password);
  const loginResponseOutcome = observePromiseOutcome(page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === state.origins.controlOrigin
      && target.pathname === "/auth/login"
      && response.request().method() === "POST";
  }, { timeout: 30_000 }));
  const meResponseOutcome = observePromiseOutcome(page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === state.origins.controlOrigin
      && target.pathname === "/auth/me"
      && response.request().method() === "GET";
  }, { timeout: 30_000 }));
  let submitFailure = null;
  try {
    await page.locator('#login-form button[type="submit"]').click();
    if (afterSubmit) await afterSubmit();
  } catch (error) {
    submitFailure = error;
  }
  // 两个 waiter 都必须被消费；submit 失败也不能让 /auth/me waiter 在关闭后游离 reject。
  const [loginOutcome, meOutcome] = await Promise.all([loginResponseOutcome, meResponseOutcome]);
  if (submitFailure) throw submitFailure;
  const loginResponse = unwrapPromiseOutcome(loginOutcome);
  assert.equal(loginResponse.status(), 200);
  const loginBody = await loginResponse.json();
  settleMutation(mutation, {
    requestId: String(loginBody.request_id ?? loginResponse.headers()["x-request-id"] ?? ""),
    outcome: "committed",
  });
  const session = registerSession(state, loginBody, {
    tenantId: state.runtime.tenantId,
    userId: role === "admin" ? state.facts.adminUserId : account.userId,
    email: account.email,
    role,
  });
  const meResponse = unwrapPromiseOutcome(meOutcome);
  assert.equal(meResponse.status(), 200);
  const meBody = await meResponse.json();
  assert.ok(
    meBody.user?.tenant_id === state.runtime.tenantId
      && meBody.user?.role === role
      && meBody.session?.session_id === session.sessionId,
  );
  await page.locator(".console-shell").waitFor({ state: "visible", timeout: 30_000 });
  // /auth/login 与 /auth/me owner 一致后才把当前 UI Session 返回给角色生命周期。
  return session;
}

/** 点击后端授权导航项并等待 active shell 与该入口冻结的真实页面标题同时收敛。 */
async function navigate(page, navigation) {
  const titleSelector = NAVIGATION_TARGET_SELECTORS[navigation];
  assert.ok(titleSelector, "浏览器验收导航目标未登记");
  const button = page.locator(`[data-console-nav="${navigation}"]`);
  await button.waitFor({ state: "visible", timeout: 30_000 });
  await button.click();
  await page.locator(`.console-shell[data-active-navigation="${navigation}"]`).waitFor({
    state: "visible",
    timeout: 30_000,
  });
  await page.locator(titleSelector).waitFor({ state: "visible", timeout: 30_000 });
  await page.waitForTimeout(150);
}

/** 读取目标 Alias 的动态表单 locator；页面局部重绘后调用方需重新调用本函数。 */
function cacheConfigRow(page, state) {
  return page.locator(`[data-cache-affinity-row="${state.runtime.aliasId}"]`);
}

/** 等待目标 Alias PAGE-072 配置行，并验证 tenant/运行时内部 ID 没有表单入口。 */
async function waitForCacheConfigRow(page, state, canWrite) {
  const row = cacheConfigRow(page, state);
  await row.waitFor({ state: "visible", timeout: 30_000 });
  assert.equal(await row.locator('[name="tenant_id"], [name="provider_credential_id"], [name="binding_id"]').count(), 0);
  assert.equal(await row.locator("[data-cache-affinity-form]").count(), canWrite ? 1 : 0);
  assert.equal(await row.locator(".cache-affinity-readonly-grid").count(), canWrite ? 0 : 1);
  return row;
}

/** 读取表单公开状态用于布尔断言；分类值不返回，防止误写验收结果。 */
async function readCacheFormPublicState(page, state) {
  const row = await waitForCacheConfigRow(page, state, true);
  const form = row.locator("[data-cache-affinity-form]");
  return {
    minimumPrefix: Number(await form.locator('[name="cacheAffinityMinimumPrefix"]').inputValue()),
    enabled: await form.locator('[name="cacheAffinityEnabled"]').isChecked(),
    busy: await form.getAttribute("aria-busy") === "true",
    saveDisabled: await form.locator('button[type="submit"]').isDisabled(),
    statusText: await form.locator("[data-cache-affinity-draft-status]").innerText(),
  };
}

/** 修改最小前缀数并等待 dirty/save 门禁更新；不触碰分类输入。 */
async function editMinimumPrefix(page, state, value) {
  const row = await waitForCacheConfigRow(page, state, true);
  const input = row.locator('[name="cacheAffinityMinimumPrefix"]');
  await input.fill(String(value));
  await page.waitForFunction(({ aliasId }) => {
    const target = document.querySelector(`[data-cache-affinity-row="${aliasId}"]`);
    const save = target?.querySelector('button[type="submit"]');
    const output = target?.querySelector("[data-cache-affinity-draft-status]");
    return save?.disabled === false && output?.textContent?.includes("未保存");
  }, { aliasId: state.runtime.aliasId });
}

/** 选择一个与当前值不同且仍在后端范围内的前缀数，避免测试依赖固定初值。 */
function nextMinimumPrefix(current, step = 1) {
  const value = Number(current);
  assert.ok(Number.isSafeInteger(value) && value >= 256 && value <= 16384);
  if (value + step <= 16384) return value + step;
  return Math.max(256, value - step);
}

/**
 * 暂缓一条精确浏览器请求；释放后 fallback 到 Context 门禁并访问真实服务，不构造响应。
 * 该通用闸门用于捕获真实请求对应的 loading 边界，目标和非目标都不会绕过全局网络策略。
 */
async function installBeforeSendBrowserRequestGate(
  page,
  state,
  routeSupervisor,
  { pathname, method },
) {
  const observed = createDeferred();
  const released = createDeferred();
  const handlerSettled = createDeferred();
  const routePattern = new URL(pathname, state.origins.controlOrigin).href;
  let matchCount = 0;
  let matchedRequest = null;
  // release 只唤醒内存 gate；真实网络仍必须继续经过 Context origin/隐私门禁。
  const release = () => released.resolve();
  const unregisterSealHook = routeSupervisor.registerSealHook(release);
  const admissionGuard = createBrowserGateAdmissionGuard();
  const handler = superviseBrowserRoute(
    routeSupervisor,
    "browser.route.before_send",
    async (route) => {
      const request = route.request();
      const target = new URL(request.url());
      if (
        target.origin !== state.origins.controlOrigin
        || target.pathname !== pathname
        || request.method() !== method
      ) {
        await fallThroughBrowserRoute(route);
        return;
      }
      matchCount += 1;
      const primaryMatch = matchCount === 1;
      if (primaryMatch) matchedRequest = request;
      try {
        assert.equal(matchCount, 1);
        observed.resolve();
        await routeSupervisor.waitForSignal(
          released.promise,
          "browser.route.before_send.release",
        );
        await fallThroughBrowserRoute(route);
      } finally {
        if (primaryMatch) handlerSettled.resolve();
      }
    },
    admissionGuard,
  );
  await page.route(routePattern, handler);
  const dispose = createBrowserGateDisposer({
    matchedRequest: () => matchedRequest,
    handlerSettled: handlerSettled.promise,
    release,
    admissionGuard,
    async unroute() {
      unregisterSealHook();
      await routeSupervisor.waitForQuiescence();
      await routeSupervisor.trackOperation(() => page.unroute(routePattern, handler));
      // 卸载后再 drain，覆盖 Playwright 已派发但尚未返回的迟到 callback。
      await routeSupervisor.waitForQuiescence();
    },
  });
  return {
    /** 页面动作启动后才创建带 rejection observer 的等待，避免动作先失败时出现悬空 Promise。 */
    waitObserved: () => routeSupervisor.waitForSignal(
      observed.promise, "browser.route.before_send.observed",
    ),
    release,
    /** 等真实请求终态后再卸载，禁止 unroute 自动推进与 Context guard 形成双处理。 */
    dispose,
  };
}

/**
 * 拦截下一次真实 CacheAffinity PATCH，在请求发出前暂缓；释放后仍由浏览器直接连接 Control API。
 * 该闸门用于稳定截图 saving 态，不返回伪造响应。
 */
async function installBeforeSendPatchGate(page, state, routeSupervisor, mutation) {
  const observed = createDeferred();
  const released = createDeferred();
  const handlerSettled = createDeferred();
  const routePattern = new URL(
    `/model-aliases/${state.runtime.aliasId}/cache-affinity`,
    state.origins.controlOrigin,
  ).href;
  let matchCount = 0;
  let matchedRequest = null;
  const release = () => released.resolve();
  const unregisterSealHook = routeSupervisor.registerSealHook(release);
  const admissionGuard = createBrowserGateAdmissionGuard();
  const handler = superviseBrowserRoute(
    routeSupervisor,
    "browser.route.before_patch",
    async (route) => {
      const request = route.request();
      const target = new URL(request.url());
      if (
        target.origin !== state.origins.controlOrigin
        || target.pathname !== `/model-aliases/${state.runtime.aliasId}/cache-affinity`
        || request.method() !== "PATCH"
      ) {
        await fallThroughBrowserRoute(route);
        return;
      }
      matchCount += 1;
      const primaryMatch = matchCount === 1;
      if (primaryMatch) matchedRequest = request;
      try {
        assert.equal(matchCount, 1);
        mutation.requestId = String(request.headers()["x-request-id"] ?? "");
        assert.ok(REQUEST_ID_PATTERN.test(mutation.requestId));
        observed.resolve();
        await routeSupervisor.waitForSignal(
          released.promise,
          "browser.route.before_patch.release",
        );
        await fallThroughBrowserRoute(route);
      } finally {
        if (primaryMatch) handlerSettled.resolve();
      }
    },
    admissionGuard,
  );
  await page.route(routePattern, handler);
  const dispose = createBrowserGateDisposer({
    matchedRequest: () => matchedRequest,
    handlerSettled: handlerSettled.promise,
    release,
    admissionGuard,
    async unroute() {
      unregisterSealHook();
      await routeSupervisor.waitForQuiescence();
      await routeSupervisor.trackOperation(() => page.unroute(routePattern, handler));
      // 迟到 callback 只能由卸载推进，返回前必须证明 supervisor 再次静止。
      await routeSupervisor.waitForQuiescence();
    },
  });
  return {
    /** click 完成事件派发后再开始有界等待，首错已发生时会立即返回同一安全错误。 */
    waitObserved: () => routeSupervisor.waitForSignal(
      observed.promise, "browser.route.before_patch.observed",
    ),
    release,
    /** 移除精确 handler，避免影响后续 PATCH。 */
    dispose,
  };
}

/**
 * 先向真实 Control API 发送 PATCH，再暂缓把真实响应交给页面，用于验证离页 Abort/owner 收敛。
 * route.fetch/fulfill 不改写状态码、Header 或正文，因此不属于 mock fetch。
 */
async function installAfterCommitPatchGate(page, state, routeSupervisor, mutation) {
  const committed = createDeferred();
  const released = createDeferred();
  const handlerSettled = createDeferred();
  const routePattern = new URL(
    `/model-aliases/${state.runtime.aliasId}/cache-affinity`,
    state.origins.controlOrigin,
  ).href;
  let matchCount = 0;
  let matchedRequest = null;
  const release = () => released.resolve();
  const unregisterSealHook = routeSupervisor.registerSealHook(release);
  const admissionGuard = createBrowserGateAdmissionGuard();
  const handler = superviseBrowserRoute(
    routeSupervisor,
    "browser.route.after_commit",
    async (route) => {
      const request = route.request();
      const target = new URL(request.url());
      if (
        target.origin !== state.origins.controlOrigin
        || target.pathname !== `/model-aliases/${state.runtime.aliasId}/cache-affinity`
        || request.method() !== "PATCH"
      ) {
        await fallThroughBrowserRoute(route);
        return;
      }
      matchCount += 1;
      const primaryMatch = matchCount === 1;
      if (primaryMatch) matchedRequest = request;
      try {
        assert.equal(matchCount, 1);
        mutation.requestId = String(request.headers()["x-request-id"] ?? "");
        assert.ok(REQUEST_ID_PATTERN.test(mutation.requestId));
        // route.fetch 绕过 Context route 链，必须显式复用相同网络和 URL 隐私门禁并禁止重定向。
        assertGuardedRouteFetchRequest(request, state);
        const upstream = await route.fetch(GUARDED_ROUTE_FETCH_OPTIONS);
        assert.equal(upstream.status(), 200);
        settleMutation(mutation, { requestId: mutation.requestId, outcome: "committed" });
        committed.resolve();
        await routeSupervisor.waitForSignal(
          released.promise,
          "browser.route.after_commit.release",
        );
        try {
          await route.fulfill({ response: upstream });
        } catch {
          // 离页触发 Abort 后 route 可能已关闭；真实上游提交已由 status=200 单独确认。
        }
      } finally {
        if (primaryMatch) handlerSettled.resolve();
      }
    },
    admissionGuard,
  );
  await page.route(routePattern, handler);
  const dispose = createBrowserGateDisposer({
    matchedRequest: () => matchedRequest,
    handlerSettled: handlerSettled.promise,
    release,
    admissionGuard,
    async unroute() {
      unregisterSealHook();
      await routeSupervisor.waitForQuiescence();
      await routeSupervisor.trackOperation(() => page.unroute(routePattern, handler));
      // owner/session 场景也必须等卸载窗口内的 callback 全部退出后才切换页面或身份。
      await routeSupervisor.waitForQuiescence();
    },
  });
  return {
    /** 只在调用方准备消费时创建 Promise，避免 route.fetch 先失败形成未处理 rejection。 */
    waitCommitted: () => routeSupervisor.waitForSignal(
      committed.promise, "browser.route.after_commit.committed",
    ),
    release,
    /** 移除精确 handler，避免 owner 测试污染下一阶段。 */
    dispose,
  };
}

/** 中断下一次真实 PATCH 连接，不伪造 HTTP 响应，用于 PAGE-072 固定 error 态。 */
async function installAbortPatchGate(page, state, routeSupervisor, mutation, evidence) {
  const observed = createDeferred();
  const handlerSettled = createDeferred();
  const routePattern = new URL(
    `/model-aliases/${state.runtime.aliasId}/cache-affinity`,
    state.origins.controlOrigin,
  ).href;
  let matchCount = 0;
  let transportWindow = null;
  let matchedRequest = null;
  const admissionGuard = createBrowserGateAdmissionGuard();
  const handler = superviseBrowserRoute(
    routeSupervisor,
    "browser.route.abort_patch",
    async (route) => {
      const request = route.request();
      const target = new URL(request.url());
      if (
        target.origin !== state.origins.controlOrigin
        || target.pathname !== `/model-aliases/${state.runtime.aliasId}/cache-affinity`
        || request.method() !== "PATCH"
      ) {
        await fallThroughBrowserRoute(route);
        return;
      }
      matchCount += 1;
      const primaryMatch = matchCount === 1;
      if (primaryMatch) matchedRequest = request;
      try {
        assert.equal(matchCount, 1);
        const requestId = String(request.headers()["x-request-id"] ?? "");
        settleMutation(mutation, { requestId, outcome: "aborted" });
        transportWindow = beginExpectedTransportConsoleError(evidence, "cache_config_write");
        observed.resolve();
        await route.abort("connectionfailed");
      } finally {
        if (primaryMatch) handlerSettled.resolve();
      }
    },
    admissionGuard,
  );
  await page.route(routePattern, handler);
  const dispose = createBrowserGateDisposer({
    matchedRequest: () => matchedRequest,
    handlerSettled: handlerSettled.promise,
    release: () => {},
    admissionGuard,
    async unroute() {
      await routeSupervisor.waitForQuiescence();
      await routeSupervisor.trackOperation(() => page.unroute(routePattern, handler));
      // transport gate 卸载完成后不得遗留会吞掉下一次 PATCH 的 callback。
      await routeSupervisor.waitForQuiescence();
    },
  });
  return {
    /** 中断动作发出后才等待命中信号；此前首错由 supervisor 保留，不会丢失。 */
    waitObserved: () => routeSupervisor.waitForSignal(
      observed.promise, "browser.route.abort_patch.observed",
    ),
    transportWindow: () => transportWindow,
    /** 移除中断 handler，保证后续 cleanup PATCH 走真实网络。 */
    dispose,
  };
}

/** 把实际响应中的版本投影回 UI 公共状态，避免在断言中保留配置正文。 */
async function waitForConfigVersion(page, state, version) {
  await page.waitForFunction(({ aliasId, expected }) => {
    const row = document.querySelector(`[data-cache-affinity-row="${aliasId}"]`);
    return row?.querySelector("[data-cache-affinity-draft-status]")?.textContent?.includes(`v${expected}`);
  }, { aliasId: state.runtime.aliasId, expected: version });
}

/** 验证 PAGE-072 empty/configured/editing/saving、owner Abort、CAS GET、error/retry/cancel。 */
async function verifyAdminConfigStates(page, state, evidence, scan, viewportMatrix) {
  await waitForCacheConfigRow(page, state, true);
  const configuredSelector = `[data-cache-affinity-row="${state.runtime.aliasId}"]`;
  const emptySelector = `[data-cache-affinity-row="${state.emptyAliasOwner.modelAliasId}"]`;
  const emptyRow = page.locator(emptySelector);
  await emptyRow.waitFor({ state: "visible", timeout: 30_000 });
  assert.match(await emptyRow.innerText(), /未配置/);
  await captureViewportState(page, state, "admin", "empty", emptySelector, scan, viewportMatrix);
  const initial = await readCacheFormPublicState(page, state);
  assert.equal(initial.enabled, state.facts.originalConfig.status === "enabled");
  assert.equal(initial.saveDisabled, true);
  await scan("page072:configured");
  await captureViewportState(
    page, state, "admin", "configured", ".routing-policies-panel", scan, viewportMatrix,
    configuredSelector,
  );

  const firstValue = nextMinimumPrefix(initial.minimumPrefix, 1);
  await editMinimumPrefix(page, state, firstValue);
  const editing = await readCacheFormPublicState(page, state);
  assert.equal(editing.minimumPrefix, firstValue);
  assert.equal(editing.saveDisabled, false);
  await scan("page072:editing");
  await captureViewportState(
    page, state, "admin", "editing", ".routing-policies-panel", scan, viewportMatrix,
    configuredSelector,
  );

  const saveMutation = registerMutation(state, "ui_save_success");
  const saveGate = await installBeforeSendPatchGate(
    page, state, evidence.routeSupervisor, saveMutation,
  );
  const saveResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === state.origins.controlOrigin
      && target.pathname === `/model-aliases/${state.runtime.aliasId}/cache-affinity`
      && response.request().method() === "PATCH";
  }, { timeout: 30_000 });
  // 转成只 resolve 的 outcome，确保 gate 先失败时 waitForResponse loser 也会被最终等待并消费。
  const saveResponseOutcome = saveResponsePromise.then(
    (response) => ({ status: "fulfilled", response }),
    (error) => ({ status: "rejected", error }),
  );
  let savedVersion;
  let saveGateStatus = null;
  try {
    await runAcceptanceStage("browser.admin.save_gate", async () => {
      await cacheConfigRow(page, state).locator('button[type="submit"]').click();
      await saveGate.waitObserved();
    });
    await runAcceptanceStage("browser.admin.saving_state", async () => {
      const saving = await readCacheFormPublicState(page, state);
      assert.equal(saving.busy, true);
      assert.match(saving.statusText, /保存/);
    });
    await runAcceptanceStage("browser.admin.saving_capture", async () => {
      await scan("page072:saving");
      await captureViewportState(
        page, state, "admin", "saving", ".routing-policies-panel", scan, viewportMatrix,
        configuredSelector,
      );
    });
    saveGate.release();
    await runAcceptanceStage("browser.admin.save_response", async () => {
      const responseOutcome = await saveResponseOutcome;
      // loser 已转为只 resolve 的 outcome，此处恢复原失败并交由当前固定阶段统一脱敏。
      if (responseOutcome.status === "rejected") throw responseOutcome.error;
      const saveResponse = responseOutcome.response;
      assert.equal(saveResponse.status(), 200);
      const savedBody = await scanTargetResponse(saveResponse, state, evidence);
      settleMutation(saveMutation, { requestId: saveMutation.requestId, outcome: "committed" });
      savedVersion = Number(savedBody.cache_affinity?.version);
      assert.ok(savedVersion > state.facts.originalConfig.version);
      await waitForConfigVersion(page, state, savedVersion);
    });
  } finally {
    // 任一页面断言失败也必须释放并卸载 gate，避免 callback 与后续 SQLite cleanup 并发。
    saveGate.release();
    saveGateStatus = await saveGate.dispose();
    await saveResponseOutcome;
  }
  assert.equal(saveGateStatus, 200, "保存请求未正常完成");
  await scan("page072:success");

  const ownerValue = nextMinimumPrefix(firstValue, 2);
  await editMinimumPrefix(page, state, ownerValue);
  const ownerMutation = registerMutation(state, "ui_owner_abort");
  const ownerGate = await installAfterCommitPatchGate(
    page, state, evidence.routeSupervisor, ownerMutation,
  );
  let ownerRequestStatus;
  try {
    await cacheConfigRow(page, state).locator('button[type="submit"]').click();
    await ownerGate.waitCommitted();
    await navigate(page, "overview");
  } finally {
    // 离页、route.fetch 或 DOM 失败均须让真实 Route 先终结，再进入下一次 PATCH。
    ownerGate.release();
    ownerRequestStatus = await ownerGate.dispose();
  }
  assert.equal(ownerRequestStatus, null, "离页未中断原 CacheAffinity Request");
  await navigate(page, "routing-policies");
  const ownerConverged = await readCacheFormPublicState(page, state);
  assert.equal(ownerConverged.minimumPrefix, ownerValue);
  assert.doesNotMatch(ownerConverged.statusText, /已保存/);
  await scan("page072:owner_converged");

  const staleDraftValue = nextMinimumPrefix(ownerValue, 3);
  await editMinimumPrefix(page, state, staleDraftValue);
  const current = await readCurrentConfig(state, state.apiTokens.get("admin"), "before_conflict");
  const externalValue = nextMinimumPrefix(ownerValue, 4);
  const external = await patchConfigApi(
    state,
    state.apiTokens.get("admin"),
    { ...configSemantics(current), minimum_prefix_chars: externalValue },
    current.version,
    "external_conflict",
  );
  const conflictMutation = registerMutation(state, "ui_conflict");
  const conflictEvidenceStart = evidence.targetResponses.length;
  // Chrome 会把已由页面正确处理的 409 也写成 Console error；只在本次精确 PATCH 证据链内接纳至多一条。
  const conflictTransportWindow = beginExpectedTransportConsoleError(evidence, "cache_config_write");
  let conflictRefresh;
  try {
    const conflictResponseOutcome = observePromiseOutcome(page.waitForResponse((response) => {
      const target = new URL(response.url());
      return target.origin === state.origins.controlOrigin
        && target.pathname === `/model-aliases/${state.runtime.aliasId}/cache-affinity`
        && response.request().method() === "PATCH";
    }, { timeout: 30_000 }));
    const conflictResponse = await runBrowserActionWithObservedResponse(
      () => cacheConfigRow(page, state).locator('button[type="submit"]').click(),
      conflictResponseOutcome,
    );
    const conflictBody = await scanTargetResponse(conflictResponse, state, evidence);
    assert.equal(conflictResponse.status(), 409);
    assert.equal(conflictBody.code, "cache_affinity.version_conflict");
    assert.ok(!Object.hasOwn(conflictBody, "details"));
    const conflictRequestId = String(conflictResponse.request().headers()["x-request-id"] ?? "");
    settleMutation(conflictMutation, { requestId: conflictRequestId, outcome: "rejected" });
    await waitForEvidence(() => {
      const records = evidence.targetResponses.slice(conflictEvidenceStart);
      const conflictIndex = records.findIndex(
        (item) => item.surface === "cache_config_write" && item.method === "PATCH" && item.status === 409,
      );
      return conflictIndex >= 0 && records.slice(conflictIndex + 1).some(
        (item) => item.surface === "model_alias_list" && item.method === "GET" && item.status === 200,
      );
    });
    await settleTargetResponseScans(evidence);
    conflictRefresh = assertConflictRefreshEvidence(
      evidence.targetResponses.slice(conflictEvidenceStart),
      external.version,
    );
    // 这里只提供有界 Chrome Console 投递宽限；上方 409 正文和后续 GET/版本证据不能由等待替代。
    await page.waitForTimeout(200);
  } finally {
    settleExpectedTransportConsoleError(evidence, conflictTransportWindow);
  }
  const rebaseButton = cacheConfigRow(page, state).locator("[data-cache-affinity-rebase]");
  await rebaseButton.waitFor({ state: "visible", timeout: 30_000 });
  assert.match(await cacheConfigRow(page, state).innerText(), /版本冲突/);
  await scan("page072:conflict");
  await captureViewportState(
    page, state, "admin", "conflict", ".routing-policies-panel", scan, viewportMatrix,
    configuredSelector,
  );
  await rebaseButton.click();
  const rebased = await readCacheFormPublicState(page, state);
  assert.equal(rebased.minimumPrefix, externalValue);
  assert.equal(rebased.saveDisabled, true);
  assert.ok(external.version > savedVersion);
  await scan("page072:rebase");

  const errorValue = nextMinimumPrefix(externalValue, 5);
  await editMinimumPrefix(page, state, errorValue);
  const errorMutation = registerMutation(state, "ui_transport_error");
  const abortGate = await installAbortPatchGate(
    page, state, evidence.routeSupervisor, errorMutation, evidence,
  );
  let transportWindow;
  let abortedRequestStatus;
  try {
    await cacheConfigRow(page, state).locator('button[type="submit"]').click();
    await abortGate.waitObserved();
    const errorBox = cacheConfigRow(page, state).locator(".cache-affinity-feedback[role='alert']");
    await errorBox.waitFor({ state: "visible", timeout: 30_000 });
    assert.match(await errorBox.innerText(), /cache_affinity\.update_failed/);
    transportWindow = abortGate.transportWindow();
  } finally {
    abortedRequestStatus = await abortGate.dispose();
  }
  assert.equal(abortedRequestStatus, null, "transport gate 未产生真实 Request 失败终态");
  await page.waitForTimeout(200);
  settleExpectedTransportConsoleError(evidence, transportWindow);
  await scan("page072:error");
  await captureViewportState(
    page, state, "admin", "error", ".routing-policies-panel", scan, viewportMatrix,
    configuredSelector,
  );

  const retryMutation = registerMutation(state, "ui_transport_retry");
  const retryResponseOutcome = observePromiseOutcome(page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === state.origins.controlOrigin
      && target.pathname === `/model-aliases/${state.runtime.aliasId}/cache-affinity`
      && response.request().method() === "PATCH";
  }, { timeout: 30_000 }));
  const retryResponse = await runBrowserActionWithObservedResponse(
    () => cacheConfigRow(page, state).locator('button[type="submit"]').click(),
    retryResponseOutcome,
  );
  assert.equal(retryResponse.status(), 200);
  const retryBody = await scanTargetResponse(retryResponse, state, evidence);
  const retryRequestId = String(retryResponse.request().headers()["x-request-id"] ?? "");
  settleMutation(retryMutation, { requestId: retryRequestId, outcome: "committed" });
  const retryVersion = Number(retryBody.cache_affinity?.version);
  assert.ok(retryVersion > external.version);
  await waitForConfigVersion(page, state, retryVersion);

  const cancelDraftValue = nextMinimumPrefix(errorValue, 6);
  await editMinimumPrefix(page, state, cancelDraftValue);
  await cacheConfigRow(page, state).locator("[data-cache-affinity-cancel]").click();
  await page.waitForTimeout(100);
  const cancelled = await readCacheFormPublicState(page, state);
  const retryCancel = assertRetryCancelEvidence({
    abortedRequestId: errorMutation.requestId,
    retryRequestId,
    committedValue: errorValue,
    cancelledDraftValue: cancelDraftValue,
    restoredValue: cancelled.minimumPrefix,
    saveDisabled: cancelled.saveDisabled,
  });
  await scan("page072:cancel_restored");
  return {
    configured: true,
    empty: true,
    editing: true,
    saving: true,
    success: true,
    ownerAbortConverged: true,
    conflict: true,
    conflictRefresh,
    rebase: true,
    error: true,
    retryCancel,
  };
}

/** 验证 RequestLog 详情只含 Selection 白名单，并投影预计复用与 cache_tokens。 */
function assertPage073DetailPayload(detail, expectation) {
  const selections = detail?.request_log?.cache_affinity_selections;
  assert.ok(Array.isArray(selections) && selections.length === 1);
  const selection = selections[0];
  assertExactSet(new Set(Object.keys(selection)), SAFE_SELECTION_FIELDS, "Selection DTO");
  assert.equal(selection.selection_reason, expectation.reason);
  assert.equal(selection.estimated_hit, expectation.estimatedHit);
  for (const forbidden of [
    "provider_credential_id_snapshot",
    "cache_affinity_binding_id_snapshot",
    "cache_affinity_scope_id_snapshot",
    "endpoint_revision_digest_snapshot",
    "affinity_scope_digest",
    "data_class",
    "key_prefix",
  ]) {
    assert.ok(!Object.hasOwn(selection, forbidden));
  }
  const usage = detail.request_log.usage;
  assert.ok(Array.isArray(usage) && usage.length >= 1);
  const cacheTokens = Number(usage[0].cache_tokens);
  assert.ok(Number.isSafeInteger(cacheTokens) && cacheTokens >= 0);
  return {
    state: expectation.state,
    estimatedHit: expectation.estimatedHit,
    cacheTokens,
    selectionReasonMatched: true,
    dtoWhitelistMatched: true,
  };
}

/** 过滤并打开一条真实 RequestLog，核对 PAGE-073 状态与 Provider 实报字段互相独立。 */
async function openAndVerifyPage073(page, state, evidence, requestKey, scan) {
  const expectation = state.facts.page073[requestKey];
  const sourceRequestId = state.runtime.requestIds[requestKey];
  await navigate(page, "request-logs");
  const form = page.locator("#request-log-filter-form");
  await form.locator('[name="requestId"]').fill(sourceRequestId);
  const listResponseOutcome = observePromiseOutcome(page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === state.origins.controlOrigin
      && target.pathname === "/request-logs"
      && target.searchParams.get("request_id") === sourceRequestId;
  }, { timeout: 30_000 }));
  const listResponse = await runBrowserActionWithObservedResponse(
    () => form.locator('button[type="submit"]').click(),
    listResponseOutcome,
  );
  assert.equal(listResponse.status(), 200);
  await scanTargetResponse(listResponse, state, evidence);
  const detailButton = page.locator(`[data-request-log-detail="${sourceRequestId}"]`);
  await detailButton.waitFor({ state: "visible", timeout: 30_000 });
  const detailResponseOutcome = observePromiseOutcome(page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === state.origins.controlOrigin
      && target.pathname === `/request-logs/${sourceRequestId}`
      && response.request().method() === "GET";
  }, { timeout: 30_000 }));
  const detailResponse = await runBrowserActionWithObservedResponse(
    () => detailButton.click(),
    detailResponseOutcome,
  );
  assert.equal(detailResponse.status(), 200);
  const detail = await scanTargetResponse(detailResponse, state, evidence);
  const projection = assertPage073DetailPayload(detail, expectation);
  assert.equal(projection.cacheTokens, expectation.cacheTokens);
  const section = page.locator(".cache-affinity-selection-section");
  await section.waitFor({ state: "visible", timeout: 30_000 });
  const sectionText = await section.innerText();
  const expectedLabel = {
    pending: "复用待确认绑定",
    warm: "预计复用绑定",
    rebound: "重新绑定",
    created: "新建绑定",
  }[expectation.state];
  assert.ok(sectionText.includes(expectedLabel));
  assert.ok(sectionText.includes(expectation.estimatedHit ? "预计复用绑定" : "未预计命中"));
  const detailText = await page.locator(".request-log-detail").innerText();
  assert.ok(detailText.includes("上游实报缓存 Token"));
  assert.ok(detailText.includes(String(expectation.cacheTokens)));
  const sectionHtml = await section.evaluate((element) => element.outerHTML);
  for (const internalId of [
    detail.request_log.cache_affinity_selections[0].cache_affinity_selection_id,
    detail.request_log.cache_affinity_selections[0].cache_affinity_id,
    detail.request_log.cache_affinity_selections[0].provider_id,
  ]) {
    assert.ok(!sectionHtml.includes(String(internalId)));
  }
  await scan(`page073:${expectation.state}`);
  return projection;
}

/** 计算布局指标并拒绝横向溢出、文本/控件裁切和非嵌套关键元素重叠。 */
async function readResponsiveLayout(page, viewport, panelSelector) {
  return page.evaluate(({ selector, expectedWidth }) => {
    const root = document.documentElement;
    const body = document.body;
    const panel = document.querySelector(selector);
    if (!panel) return { missing: true };
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rectangle = element.getBoundingClientRect();
      const visuallyHidden = style.position === "absolute"
        && ["hidden", "clip"].includes(style.overflowX)
        && ["hidden", "clip"].includes(style.overflowY)
        && (style.clipPath !== "none" || (style.clip !== "auto" && style.clip !== ""));
      // 响应式表头使用标准 1x1 clip 技法保留无障碍语义；它不属于用户可见的裁切内容。
      return style.display !== "none"
        && style.visibility !== "hidden"
        && rectangle.width > 0
        && rectangle.height > 0
        && !visuallyHidden;
    };
    const panelRectangle = panel.getBoundingClientRect();
    const inspectionSelector = [
      "button", "input", "select", "textarea", "output", "label", "[role='row']", "fieldset",
      "section", "article", "dl", "dt", "dd", "h1", "h2", "h3", "p",
      ".cache-affinity-feedback", ".empty-state",
    ].join(",");
    const nodes = [...panel.querySelectorAll(inspectionSelector)]
      .filter(visible);
    const horizontalOffenderCount = nodes.filter((element) => {
      const rectangle = element.getBoundingClientRect();
      return rectangle.left < -1 || rectangle.right > root.clientWidth + 1;
    }).length;
    const clippedControlCount = [...panel.querySelectorAll("button,select")]
      .filter(visible)
      .filter((element) => element.scrollWidth > element.clientWidth + 1).length;
    const clippedElements = nodes.filter((element) => {
      if (element.matches("input,textarea")) return false;
      const style = getComputedStyle(element);
      const horizontalClip = element.scrollWidth > element.clientWidth + 1;
      const verticalClip = ["hidden", "clip"].includes(style.overflowY)
        && element.scrollHeight > element.clientHeight + 1;
      return horizontalClip || verticalClip;
    });
    const safeTagKinds = new Set([
      "button", "select", "output", "label", "fieldset", "section", "article", "dl", "dt", "dd",
      "h1", "h2", "h3", "p",
    ]);
    const clippedElementKinds = [...new Set(clippedElements.map((element) => {
      if (element.matches(".routing-policy-head")) return "routing_policy_head";
      if (element.matches(".cache-affinity-feedback")) return "cache_affinity_feedback";
      if (element.matches(".empty-state")) return "empty_state";
      // role=row 可能由非表格标签承载，单独归类才能与普通 section/article 裁切区分。
      if (element.matches("[role='row']")) return "role_row";
      const tag = element.tagName.toLowerCase();
      return safeTagKinds.has(tag) ? tag : "other";
    }))].sort();
    const rectangles = nodes
      .map((element) => ({ element, rectangle: element.getBoundingClientRect() }));
    let overlapCount = 0;
    for (let left = 0; left < rectangles.length; left += 1) {
      for (let right = left + 1; right < rectangles.length; right += 1) {
        const first = rectangles[left];
        const second = rectangles[right];
        if (first.element.contains(second.element) || second.element.contains(first.element)) continue;
        const overlapWidth = Math.min(first.rectangle.right, second.rectangle.right)
          - Math.max(first.rectangle.left, second.rectangle.left);
        const overlapHeight = Math.min(first.rectangle.bottom, second.rectangle.bottom)
          - Math.max(first.rectangle.top, second.rectangle.top);
        if (overlapWidth > 2 && overlapHeight > 2) overlapCount += 1;
      }
    }
    return {
      missing: false,
      innerWidth: window.innerWidth,
      expectedWidth,
      rootScrollWidth: root.scrollWidth,
      bodyScrollWidth: body.scrollWidth,
      panelScrollWidth: panel.scrollWidth,
      panelClientWidth: panel.clientWidth,
      panelLeft: Math.round(panelRectangle.left),
      panelRight: Math.round(panelRectangle.right),
      horizontalOffenderCount,
      clippedControlCount,
      clippedElementCount: clippedElements.length,
      clippedElementKinds,
      overlapCount,
      inspectedElementCount: nodes.length,
    };
  }, { selector: panelSelector, expectedWidth: viewport.width });
}

/** 布局失败只公开闭集 metric 名，不输出可能受 DOM 内容影响的像素值或元素信息。 */
function assertLayoutMetric(condition, metric) {
  assert.ok(SAFE_CODE_PATTERN.test(metric), "布局 metric 非安全值");
  if (!condition) throw new BrowserAcceptanceStageError(`browser.layout.${metric}`, "contract_failed");
}

/** 对纯布局指标执行失败关闭断言，供浏览器与轻量合同测试共用。 */
function assertLayoutMetrics(metrics, viewport) {
  assertLayoutMetric(metrics.missing === false, "panel_missing");
  assertLayoutMetric(metrics.innerWidth === viewport.width, "viewport_width");
  assertLayoutMetric(metrics.rootScrollWidth <= viewport.width, "root_overflow");
  assertLayoutMetric(metrics.bodyScrollWidth <= viewport.width, "body_overflow");
  assertLayoutMetric(
    metrics.panelScrollWidth <= metrics.panelClientWidth + 1,
    "panel_overflow",
  );
  assertLayoutMetric(
    metrics.panelLeft >= -1 && metrics.panelRight <= viewport.width + 1,
    "panel_bounds",
  );
  assertLayoutMetric(metrics.horizontalOffenderCount === 0, "horizontal_offender");
  assertLayoutMetric(metrics.clippedControlCount === 0, "clipped_control");
  const clippedKind = Array.isArray(metrics.clippedElementKinds)
    ? metrics.clippedElementKinds.find((kind) => SAFE_CODE_PATTERN.test(kind))
    : null;
  assertLayoutMetric(
    metrics.clippedElementCount === 0,
    `clipped_element_${clippedKind ?? "unknown"}`,
  );
  assertLayoutMetric(metrics.overlapCount === 0, "sibling_overlap");
  assertLayoutMetric(metrics.inspectedElementCount > 0, "inspection_empty");
  return { horizontalOverflow: false, clippedElementCount: 0, overlapCount: 0 };
}

/** 在指定视口验证布局，返回不含 DOM 文本的安全计数。 */
async function assertResponsiveLayout(page, viewport, panelSelector) {
  return assertLayoutMetrics(await readResponsiveLayout(page, viewport, panelSelector), viewport);
}

/** 收集需要截图遮罩的分类值、Virtual Key 行和用户菜单；遮罩 locator 本身不写结果。 */
async function screenshotMasks(page) {
  const masks = [page.locator(".user-menu")];
  const passwordInputs = page.locator('input[type="password"]');
  if (await passwordInputs.count()) masks.push(passwordInputs);
  const classificationInputs = page.locator('[name="cacheAffinityDataClass"]');
  if (await classificationInputs.count()) masks.push(classificationInputs);
  const classificationTerms = page.locator(".cache-affinity-readonly-grid dt", { hasText: "分类" });
  if (await classificationTerms.count()) {
    masks.push(classificationTerms);
    masks.push(classificationTerms.locator("xpath=following-sibling::dd[1]"));
  }
  const virtualKeyTerms = page.locator(".request-log-detail-grid > dt", { hasText: "Virtual Key" });
  if (await virtualKeyTerms.count()) {
    masks.push(virtualKeyTerms);
    masks.push(virtualKeyTerms.locator("xpath=following-sibling::dd[1]"));
  }
  return masks;
}

/**
 * 为截图证据生成闭集角色标记样式。
 *
 * 用户菜单必须整体遮罩，移动端滚动到同一只读详情后 admin 与 developer 可能得到完全
 * 相同的安全像素。角色标记只由固定 RBAC 闭集生成并通过 Playwright screenshot style
 * 临时注入，不改业务 DOM、布局测量、Storage 或网络请求，也不会携带邮箱等身份信息。
 */
function screenshotRoleStyle(role) {
  const label = {
    admin: "role: admin",
    developer: "role: developer",
    viewer: "role: viewer",
  }[role];
  assert.ok(label, "截图角色必须属于固定 RBAC 闭集");
  return `
    html::after {
      content: "${label}";
      position: fixed;
      right: 6px;
      bottom: 6px;
      z-index: 2147483647;
      padding: 2px 5px;
      border: 1px solid #9ca3af;
      border-radius: 2px;
      background: #ffffff;
      color: #111827;
      font: 600 10px/1.2 ui-monospace, monospace;
      pointer-events: none;
    }
  `;
}

/**
 * 截图前把当前真实状态节点定位到视口，再复扫隐私并验证 PNG 非空。
 * 使用视口截图而不是整页截图，避免同页同时存在 empty/configured 时写出内容完全相同的伪状态证据。
 */
async function capturePage(page, state, role, pageName, viewport, scan, focusSelector = "main") {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  const focus = page.locator(focusSelector).first();
  await focus.waitFor({ state: "visible", timeout: 30_000 });
  await focus.scrollIntoViewIfNeeded();
  await focus.evaluate((element) => {
    const rectangle = element.getBoundingClientRect();
    const documentRoot = document.documentElement;
    const maximumScroll = Math.max(0, documentRoot.scrollHeight - window.innerHeight);
    // 小状态块放在标题下方，超高面板则从顶部开始；只改变滚动位置，不改写业务 DOM。
    const desiredTop = rectangle.height < window.innerHeight ? 96 : 16;
    const absoluteTop = window.scrollY + rectangle.top;
    window.scrollTo({
      top: Math.max(0, Math.min(maximumScroll, absoluteTop - desiredTop)),
      behavior: "instant",
    });
  });
  await scan(`${pageName}:${viewport.name}:before`);
  const buffer = await page.screenshot({
    fullPage: false,
    mask: await screenshotMasks(page),
    maskColor: "#e5e7eb",
    style: screenshotRoleStyle(role),
  });
  const pixels = inspectPngPixels(buffer, { expectedWidth: viewport.width, minHeight: viewport.height });
  await scan(`${pageName}:${viewport.name}:after`);
  const screenshotHash = createHash("sha256").update(buffer).digest("hex");
  const screenshotBytes = buffer.byteLength;
  // 最终 closeout 会再次逐文件复核；这里先失败关闭，防止重复 PNG 被写入后继续跑昂贵角色流程。
  assert.equal(state.screenshotHashes.has(screenshotHash), false, "浏览器状态截图内容重复");
  state.screenshotHashes.add(screenshotHash);
  const filename = `${role}-${pageName}-${viewport.name}.png`;
  // 每张 PNG 都是本批次的一次性证据；独占创建可让重入或并发执行在覆盖旧证据前失败。
  await writeNewEvidenceFile(path.join(state.args.browserDirectory, filename), buffer);
  return {
    filename,
    viewportName: viewport.name,
    sha256: screenshotHash,
    bytes: screenshotBytes,
    ...pixels,
    privacyMaskApplied: true,
  };
}

/** 对一个真实 DOM 状态执行三视口布局、隐私与 PNG 验收，并写入状态矩阵。 */
async function captureViewportState(
  page,
  state,
  role,
  stateKey,
  panelSelector,
  scan,
  matrix,
  screenshotFocusSelector = panelSelector,
) {
  assert.ok(REQUIRED_VIEWPORT_STATES.includes(stateKey));
  const layouts = {};
  const screenshots = [];
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.evaluate(() => window.scrollTo(0, 0));
    const stageRoot = `browser.viewport.${stateKey}.${viewport.name}`;
    // stateKey/viewport 均来自冻结闭集，固定子阶段只定位 layout/capture，不包含 DOM 或业务值。
    layouts[viewport.name] = await runAcceptanceStage(
      `${stageRoot}.layout`,
      () => assertResponsiveLayout(page, viewport, panelSelector),
    );
    screenshots.push(await runAcceptanceStage(
      `${stageRoot}.capture`,
      () => capturePage(
        page,
        state,
        role,
        `page-072-${stateKey}`,
        viewport,
        scan,
        screenshotFocusSelector,
      ),
    ));
  }
  matrix[stateKey] = { layouts, screenshots };
  return matrix[stateKey];
}

/**
 * 锁定 Browser 必须真实覆盖的 loading/empty/PAGE-072 五态与 PAGE-073 三态。
 * 每一态都必须包含三种精确视口和三张已解析 PNG，避免仅返回布尔值冒充视觉证据。
 */
function assertViewportStateMatrix(matrix) {
  assertExactSet(new Set(Object.keys(matrix ?? {})), new Set(REQUIRED_VIEWPORT_STATES), "Browser 状态矩阵");
  const expectedViewportNames = new Set(VIEWPORTS.map((item) => item.name));
  for (const stateKey of REQUIRED_VIEWPORT_STATES) {
    const entry = matrix[stateKey];
    assertExactSet(new Set(Object.keys(entry?.layouts ?? {})), expectedViewportNames, `${stateKey} layout`);
    assert.equal(entry?.screenshots?.length, VIEWPORTS.length);
    assertExactSet(
      new Set(entry.screenshots.map((item) => item.viewportName)),
      expectedViewportNames,
      `${stateKey} screenshot`,
    );
    assert.ok(entry.screenshots.every((item) => (
      item.nonBlank
      && item.privacyMaskApplied
      && SHA256_HEX_PATTERN.test(String(item.sha256 ?? ""))
      && Number.isSafeInteger(item.bytes)
      && item.bytes > 0
    )));
  }
  return { stateCount: REQUIRED_VIEWPORT_STATES.length, viewportCount: VIEWPORTS.length };
}

/** 对 reader 的 PAGE-072 与 PAGE-073 在三个视口执行布局和非空截图。 */
async function captureReaderPages(
  page,
  state,
  role,
  page073,
  scan,
  { viewportMatrix = null, includePage072 = true } = {},
) {
  const screenshots = [];
  const layouts = { page072: {}, page073: {} };
  // developer 在进入本函数前已经等待只读配置行完成；再次点击当前导航会触发 loading 重绘，
  // 使首张截图持有即将被替换的 DOM。仅 admin 的 PAGE-073-only 流程需要先从日志页返回路由页。
  if (!includePage072) {
    await runAcceptanceStage(
      `browser.reader.${role}.page072.navigate`,
      () => navigate(page, "routing-policies"),
    );
  }
  if (includePage072) {
    // 重新读取当前 DOM 的只读行，确保截图绑定的是本轮已收敛页面，而不是导航 loading 中间态。
    await runAcceptanceStage(
      `browser.reader.${role}.page072.ready`,
      () => waitForCacheConfigRow(page, state, role === "admin"),
    );
    for (const viewport of VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const stageRoot = `browser.reader.${role}.page072.${viewport.name}`;
      layouts.page072[viewport.name] = await runAcceptanceStage(
        `${stageRoot}.layout`,
        () => assertResponsiveLayout(page, viewport, ".routing-policies-panel"),
      );
      screenshots.push(await runAcceptanceStage(
        `${stageRoot}.capture`,
        () => capturePage(
          page,
          state,
          role,
          "page-072",
          viewport,
          scan,
          `[data-cache-affinity-row="${state.runtime.aliasId}"]`,
        ),
      ));
    }
  }
  for (const requestKey of ["cache_affinity_pending", "cache_affinity_warm", "cache_affinity_rebound"]) {
    const projection = await openAndVerifyPage073(page, state, page073.evidence, requestKey, scan);
    page073.states[projection.state] = projection;
    layouts.page073[projection.state] = {};
    const stateScreenshots = [];
    for (const viewport of VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      layouts.page073[projection.state][viewport.name] = await assertResponsiveLayout(
        page,
        viewport,
        ".request-logs-panel",
      );
      const screenshot = await capturePage(
        page,
        state,
        role,
        `page-073-${projection.state}`,
        viewport,
        scan,
        ".cache-affinity-selection-section",
      );
      screenshots.push(screenshot);
      stateScreenshots.push(screenshot);
    }
    if (viewportMatrix) viewportMatrix[`page073_${projection.state}`] = {
      layouts: layouts.page073[projection.state], screenshots: stateScreenshots,
    };
  }
  return { screenshots, layouts };
}

/**
 * 纯函数核对 reader 必须读取两页、developer 零写、admin 恰有三个受控中断写；viewer 零目标请求。
 * Chrome/Playwright 对浏览器内部 CORS preflight 的事件暴露并不稳定，所以 OPTIONS
 * 不是数量门禁；一旦观察到仍须完整校验，而每条非 OPTIONS 响应必须独立证明精确 ACAO。
 */
function assertRoleTargetEvidence(role, evidence) {
  const requests = evidence.targetRequests ?? [];
  const responses = evidence.targetResponses ?? [];
  assert.ok(["admin", "developer", "viewer"].includes(role), "未知浏览器验收角色");
  assert.ok(Array.isArray(requests) && Array.isArray(responses));
  // viewer 必须在前端保持目标 API 请求和响应都为零，后端 RBAC 另有独立探针。
  if (role === "viewer") {
    assert.equal(requests.length, 0);
    assert.equal(responses.length, 0);
    return {
      targetRequestCount: 0,
      targetResponseCount: 0,
      targetCorsResponseCount: 0,
      targetWriteCount: 0,
      targetWriteResponseCount: 0,
      targetInterruptedWriteCount: 0,
      targetPreflightCount: 0,
      targetPrefetchCount: 0,
    };
  }

  const readSurfaces = ["model_alias_list", "request_log_list", "request_log_detail"];
  const allowedSurfaces = new Set([...readSurfaces, "cache_config_write"]);
  const requestByIndex = new Map();
  for (const [expectedIndex, item] of requests.entries()) {
    // requestIndex 是从零开始的角色内序号；连续性既证明来源是本次监听器，也避免任意内部 ID 混入。
    assert.equal(item.requestIndex, expectedIndex);
    assert.ok(!requestByIndex.has(item.requestIndex));
    assert.ok(allowedSurfaces.has(item.surface));
    assert.ok(["GET", "PATCH", "OPTIONS"].includes(item.method));
    if (item.method === "GET") assert.ok(readSurfaces.includes(item.surface));
    if (item.method === "PATCH") assert.equal(item.surface, "cache_config_write");
    // 每条业务请求（包括三个无响应受控 PATCH）都必须独立证明请求 Origin；OPTIONS
    // 则保持 null，并由其响应扫描的完整 preflight 合同校验同一请求 Header。
    assert.equal(item.requestCorsOriginValid, item.method === "OPTIONS" ? null : true);
    requestByIndex.set(item.requestIndex, item);
  }

  const respondedRequestIndexes = new Set();
  for (const [expectedResponseIndex, item] of responses.entries()) {
    // 每条响应必须唯一引用一个已观察请求；重复响应和凭空响应都失败关闭。
    assert.equal(item.responseIndex, expectedResponseIndex);
    assert.ok(Number.isInteger(item.requestIndex));
    const pairedRequest = requestByIndex.get(item.requestIndex);
    assert.ok(pairedRequest, "目标响应缺少配对请求");
    assert.ok(!respondedRequestIndexes.has(item.requestIndex), "同一目标请求出现重复响应");
    respondedRequestIndexes.add(item.requestIndex);
    assert.equal(item.method, pairedRequest.method);
    assert.equal(item.surface, pairedRequest.surface);
    if (item.method === "GET") assert.equal(item.status, 200);
    if (item.method === "PATCH") assert.ok([200, 409].includes(item.status));
    if (item.method === "OPTIONS") assert.equal(item.status, 200);
    assert.equal(item.noStore, true);
    assert.equal(item.bodyPrivacyScanned, true);
    if (item.method === "OPTIONS") {
      assert.equal(item.bodyJsonValid, null);
      assert.equal(item.preflightHeadersValid, true);
      assert.equal(item.preflightBodyValid, true);
      assert.equal(item.corsOriginValid, null);
    } else {
      assert.equal(item.requestIdPresent, true);
      assert.equal(item.traceIdPresent, true);
      assert.equal(item.bodyJsonValid, true);
      assert.equal(item.preflightHeadersValid, null);
      assert.equal(item.preflightBodyValid, null);
      assert.equal(item.corsOriginValid, true);
    }
  }

  // 固定管理员流程的 owner after-commit、transport abort、session-switch 各有一个
  // request.response()===null 的受控 PATCH；除此之外所有目标请求都必须取得唯一响应。
  const unansweredRequests = requests.filter(
    (item) => !respondedRequestIndexes.has(item.requestIndex),
  );
  // developer 不允许无响应目标请求；admin 必须与三个固定受控中断一一对应。
  if (role === "developer") {
    assert.equal(unansweredRequests.length, 0, "developer 的每个目标读请求都必须获得响应");
  } else {
    assert.equal(unansweredRequests.length, 3, "admin 必须证明三个固定受控中断 PATCH");
    for (const item of unansweredRequests) {
      assert.equal(item.method, "PATCH");
      assert.equal(item.surface, "cache_config_write");
    }
  }

  for (const surface of readSurfaces) {
    assert.ok(responses.some(
      (item) => item.method === "GET" && item.surface === surface && item.status === 200,
    ), `缺少 ${surface} GET 200 响应`);
  }
  const writes = requests.filter(
    (item) => item.surface === "cache_config_write" && item.method === "PATCH",
  );
  const writeResponses = responses.filter(
    (item) => item.surface === "cache_config_write" && item.method === "PATCH",
  );
  // developer 的任何 CacheAffinity PATCH 都说明前端只读边界失效；管理员必须同时证明
  // 两次成功提交和一次乐观锁冲突，不能用请求数量替代响应事实。
  if (role === "developer") {
    assert.equal(writes.length, 0);
    assert.equal(writeResponses.length, 0);
  } else {
    assert.ok(writeResponses.filter((item) => item.status === 200).length >= 2);
    assert.ok(writeResponses.some((item) => item.status === 409));
  }
  const preflights = responses.filter((item) => item.method === "OPTIONS");
  const corsResponses = responses.filter((item) => item.method !== "OPTIONS");
  return {
    targetRequestCount: requests.length,
    targetResponseCount: responses.length,
    targetCorsResponseCount: corsResponses.length,
    targetWriteCount: writes.length,
    targetWriteResponseCount: writeResponses.length,
    targetInterruptedWriteCount: unansweredRequests.length,
    targetPreflightCount: preflights.length,
    targetPrefetchCount: 0,
  };
}

/** 最多两次执行真实注销，只有 HTTP 200 且登录页/Storage 收敛才算成功。 */
async function executeLogoutWithRetry({ attemptAction, verifyAction, maxAttempts = 2 }) {
  assert.ok(Number.isInteger(maxAttempts) && maxAttempts >= 1 && maxAttempts <= 3);
  const audit = { completed: false, attemptCount: 0, responseStatuses: [], authStorageCleared: false };
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    audit.attemptCount = attempt;
    let outcome;
    try {
      outcome = await attemptAction(attempt);
    } catch {
      audit.responseStatuses.push("attempt_error");
      continue;
    }
    const status = Number.isInteger(outcome?.status) ? outcome.status : null;
    audit.responseStatuses.push(status ?? "missing_response");
    if (status !== 200) continue;
    await verifyAction();
    audit.completed = true;
    audit.authStorageCleared = true;
    return audit;
  }
  // 有限重试耗尽后必须失败，不能仅依赖 Context 关闭掩盖后端 Session 未注销。
  throw new Error("注销在有限重试后仍未成功");
}

/** 点击真实 UI 注销，并复核登录页和两个认证 Storage 槽均已清除。 */
async function logoutStrict(page, state, evidence, session) {
  return executeLogoutWithRetry({
    /** 每次尝试都绑定精确 /auth/logout 响应，不读取或保存响应正文。 */
    async attemptAction() {
      const button = page.locator("#logout-button");
      if ((await button.count()) !== 1) return { status: null };
      const responseOutcome = observePromiseOutcome(page.waitForResponse((response) => {
        const target = new URL(response.url());
        return target.origin === state.origins.controlOrigin
          && target.pathname === "/auth/logout"
          && response.request().method() === "POST";
      }, { timeout: 10_000 }));
      const response = await runBrowserActionWithObservedResponse(
        () => button.click({ timeout: 10_000 }),
        responseOutcome,
      );
      return { status: response.status() };
    },
    /** HTTP 200 后仍需等待登录页，并确认 Token/Context 不再留在 Storage。 */
    async verifyAction() {
      await page.locator("#login-form").waitFor({ state: "visible", timeout: 10_000 });
      await scanBrowserCheckpoint(page, evidence, state.secretRegistry, {
        activeToken: null,
        loggedOut: true,
        label: `logout:${session.sessionId ? "owned" : "unknown"}`,
      });
    },
  });
}

/**
 * 让 Context close 已触发的 Playwright Console/PageError 监听器完成当前事件队列。
 * Context close 是网络终态，额外的一轮 setImmediate 只排空已到达 Node 的观察事件，不用固定 sleep 猜时序。
 */
async function settleBrowserObserverQueue() {
  await new Promise((resolve) => setImmediate(resolve));
}

/** 统一执行角色主流程、finally 注销、Context 关闭与最终证据门禁，任一失败都阻止部分通过。 */
async function runRoleLifecycle({
  stagePrefix,
  executeAction,
  logoutAction,
  closeAction,
  finalEvidenceAction = null,
  routeSupervisor = null,
}) {
  let result;
  let logout;
  const failures = [];
  /** 同一安全 stage/code 只登记一次，避免 gate wait 与最终 throwIfFailed 重复报错。 */
  const addFailure = (error, stage, fallbackCode) => {
    const safe = toSafeStageError(error, stage, fallbackCode);
    if (!failures.some((item) => item.stage === safe.stage && item.code === safe.code)) {
      failures.push(safe);
    }
  };
  try {
    result = await executeAction();
  } catch (error) {
    addFailure(error, `${stagePrefix}.page`, "page_failed");
  }
  try {
    logout = await logoutAction();
  } catch (error) {
    addFailure(error, `${stagePrefix}.logout`, "logout_failed");
  }
  // 有 supervisor 时无论页面是否失败都执行 close 前封口与 drain，避免 callback 被直接截断。
  if (routeSupervisor) {
    // 页面和注销均已收敛后先封口；同步钩子会释放遗留 gate，后续请求只能进入中断分支。
    routeSupervisor.seal();
    try {
      await routeSupervisor.waitForQuiescence();
    } catch (error) {
      addFailure(error, `${stagePrefix}.route_preclose`, "route_quiescence_failed");
    }
    try {
      routeSupervisor.throwIfFailed();
    } catch (error) {
      addFailure(error, `${stagePrefix}.route`, "route_handler_failed");
    }
  }
  try {
    await closeAction();
  } catch (error) {
    addFailure(error, `${stagePrefix}.context_close`, "context_close_failed");
  }
  // Context close 期间仍可能产生封口后的 abort callback，因此必须再次证明 active=0。
  if (routeSupervisor) {
    try {
      // Context 已关闭后必须真正等到 active=0；若永不收敛则由外层 runner 超时终止，禁止进入 SQLite cleanup。
      await routeSupervisor.waitForQuiescence(null);
    } catch (error) {
      addFailure(error, `${stagePrefix}.route_postclose`, "route_quiescence_failed");
    }
    try {
      // 无论 pre-drain 是否超时都再次读取首错，避免 quiescence 错误遮蔽真实 Route 失败。
      routeSupervisor.throwIfFailed();
    } catch (error) {
      addFailure(error, `${stagePrefix}.route`, "route_handler_failed");
    }
  }
  if (finalEvidenceAction) {
    try {
      // 只能在 Context close 和 Route post-close drain 之后终检，保证监听数组不再被在途请求继续改写。
      await settleBrowserObserverQueue();
      await finalEvidenceAction();
    } catch (error) {
      addFailure(error, `${stagePrefix}.evidence_final`, "evidence_final_failed");
    }
  }
  // 页面、注销和 Context 关闭任一失败都会聚合，禁止返回角色部分成功。
  if (failures.length) throw new AggregateError(failures, "浏览器角色生命周期失败");
  return { ...result, logout };
}

/** admin 完成 PAGE-072 全状态和 PAGE-073 三状态，再执行三视口视觉验收。 */
async function runAdminRole(browser, state) {
  const role = "admin";
  const context = await browser.newContext({
    viewport: VIEWPORTS[0],
    userAgent: `prd050-browser-ui-${role}-${state.browserSuffix}`,
  });
  const page = await context.newPage();
  const evidence = await installBrowserGuards(context, page, state, role);
  let session = null;
  return runRoleLifecycle({
    stagePrefix: role,
    routeSupervisor: evidence.routeSupervisor,
    /** 执行管理员页面状态、日志详情、布局和隐私检查。 */
    async executeAction() {
      const viewportMatrix = {};
      session = await loginPage(page, state, role, "overview");
      // 管理员每次检查都读取当前可变 session，Session 切换后不会继续豁免旧 Token/owner。
      const scan = (label) => scanBrowserCheckpoint(page, evidence, state.secretRegistry, {
        activeToken: session.token,
        expectedOwner: session.owner,
        label: `${role}:${label}`,
      });
      const loadingGate = await installBeforeSendBrowserRequestGate(
        page,
        state,
        evidence.routeSupervisor,
        { pathname: "/model-aliases", method: "GET" },
      );
      let page072Loading;
      let loadingResponseStatus = null;
      try {
        const loadingOutcomes = await Promise.allSettled([
          navigate(page, "routing-policies"),
          loadingGate.waitObserved(),
        ]);
        const loadingFailures = loadingOutcomes
          .filter((outcome) => outcome.status === "rejected")
          .map((outcome) => outcome.reason);
        // 两个并发分支必须都收敛后才抛错，禁止 navigation loser 与 Context close 并发。
        if (loadingFailures.length) {
          throw new AggregateError(loadingFailures, "PAGE-072 loading 并发阶段失败");
        }
        const loadingDom = await page.evaluate(() => {
          const panel = document.querySelector(".routing-policies-panel");
          return {
            activeNavigationId: document.querySelector(".console-shell")
              ?.getAttribute("data-active-navigation") ?? "",
            activeNavigationButtonCount: document.querySelectorAll(
              '[data-console-nav="routing-policies"][aria-current="page"]',
            ).length,
            panelCount: document.querySelectorAll(".routing-policies-panel").length,
            titleText: panel?.querySelector("#routing-policies-title")?.textContent?.trim() ?? "",
            loadingText: panel?.querySelector(".empty-state")?.textContent?.trim() ?? "",
            loginFormCount: document.querySelectorAll("#login-form").length,
            configurationRowCount: panel?.querySelectorAll(
              "[data-routing-policy-row], [data-cache-affinity-row]",
            ).length ?? 0,
            editorFormCount: panel?.querySelectorAll(
              "[data-routing-policy-form], [data-cache-affinity-form]",
            ).length ?? 0,
          };
        });
        page072Loading = assertPage072LoadingEvidence(loadingDom);
        await scan("page072:loading");
        await captureViewportState(
          page, state, role, "loading", ".routing-policies-panel", scan, viewportMatrix,
        );
      } finally {
        loadingGate.release();
        loadingResponseStatus = await loadingGate.dispose();
      }
      assert.equal(loadingResponseStatus, 200, "PAGE-072 loading 请求未返回 200");
      await waitForCacheConfigRow(page, state, true);
      const page072 = {
        ...(await verifyAdminConfigStates(page, state, evidence, scan, viewportMatrix)),
        loading: page072Loading,
      };

      const sessionBase = await readCacheFormPublicState(page, state);
      const sessionSwitchValue = nextMinimumPrefix(sessionBase.minimumPrefix, 7);
      await editMinimumPrefix(page, state, sessionSwitchValue);
      const sessionMutation = registerMutation(state, "ui_session_switch_abort");
      const sessionGate = await installAfterCommitPatchGate(
        page, state, evidence.routeSupervisor, sessionMutation,
      );
      let priorSession;
      let interimLogout;
      let sessionRequestStatus;
      try {
        await cacheConfigRow(page, state).locator('button[type="submit"]').click();
        await sessionGate.waitCommitted();
        priorSession = session;
        interimLogout = await logoutStrict(page, state, evidence, priorSession);
      } finally {
        // committed 之前失败也必须释放并卸载，不能把 route.fetch callback 留给外层 cleanup。
        sessionGate.release();
        sessionRequestStatus = await sessionGate.dispose();
      }
      assert.equal(sessionRequestStatus, null, "Session 切换未中断原 CacheAffinity Request");
      await page.waitForTimeout(200);
      await page.locator("#login-form").waitFor({ state: "visible", timeout: 10_000 });
      await scanBrowserCheckpoint(page, evidence, state.secretRegistry, {
        loggedOut: true,
        label: `${role}:session_switch_late_response`,
      });
      session = await loginPage(page, state, role, "routing-policies");
      await waitForCacheConfigRow(page, state, true);
      const sessionConverged = await readCacheFormPublicState(page, state);
      page072.sessionSwitch = assertSessionSwitchEvidence({
        priorSessionId: priorSession.sessionId,
        nextSessionId: session.sessionId,
        logoutCompleted: interimLogout.completed,
        authStorageCleared: interimLogout.authStorageCleared,
        // response()==null 是真实页面 Request 已失败的证据，不能再用常量 true 伪造迟到响应隔离。
        lateResponseIgnored: sessionRequestStatus === null,
        committedValue: sessionSwitchValue,
        convergedValue: sessionConverged.minimumPrefix,
      });
      await scan("page072:session_switch_converged");

      const page073 = { states: {}, evidence };
      // created 详情不截图，只用于冻结 false + 正 cache_tokens 的独立性反例。
      const created = await openAndVerifyPage073(
        page,
        state,
        evidence,
        "cache_affinity_created",
        scan,
      );
      page073.states.created = created;
      await captureReaderPages(page, state, role, page073, scan, {
        viewportMatrix,
        includePage072: false,
      });
      assertCacheEvidenceIndependent({ created, warm: page073.states.warm });
      const matrixEvidence = assertViewportStateMatrix(viewportMatrix);
      const matrixScreenshots = Object.values(viewportMatrix).flatMap((entry) => entry.screenshots);
      const privacy = await scan("final");
      const target = assertRoleTargetEvidence(role, evidence);
      return {
        role,
        page072,
        page073: { states: page073.states, estimateAndProviderReportIndependent: true },
        layouts: { stateMatrix: Object.fromEntries(
          Object.entries(viewportMatrix).map(([key, value]) => [key, value.layouts]),
        ) },
        screenshots: matrixScreenshots,
        screenshotCount: matrixScreenshots.length,
        matrixEvidence,
        privacy,
        privacyCheckpointCount: evidence.checkpointCount,
        bodyPrivacyScanCount: evidence.bodyPrivacyScanCount,
        externalRequestCount: evidence.blockedRequestCount,
        browserErrorCount: evidence.browserErrors.length,
        ...target,
      };
    },
    /** 登录成功后必须走真实 UI 注销；失败仍由外层 SQLite owner 兜底。 */
    async logoutAction() {
      if (!session) return { completed: true, required: false, attemptCount: 0, responseStatuses: [], authStorageCleared: true };
      return { required: true, ...await logoutStrict(page, state, evidence, session) };
    },
    /** Context 关闭是角色隔离硬门禁，禁止 Storage 跨角色复用。 */
    async closeAction() { await context.close(); },
    /** Context 关闭后复核全部非 DOM 守卫，迟到 Console/PageError 或响应扫描均不得形成假通过。 */
    finalEvidenceAction: () => assertBrowserGuardEvidence(evidence),
  });
}

/** developer 只读 PAGE-072，并读取 PAGE-073 pending/warm/rebound；不得出现任何 PATCH。 */
async function runDeveloperRole(browser, state) {
  const role = "developer";
  const context = await browser.newContext({
    viewport: VIEWPORTS[0],
    userAgent: `prd050-browser-ui-${role}-${state.browserSuffix}`,
  });
  const page = await context.newPage();
  const evidence = await installBrowserGuards(context, page, state, role);
  let session = null;
  return runRoleLifecycle({
    stagePrefix: role,
    routeSupervisor: evidence.routeSupervisor,
    /** 核对只读 DOM、真实详情与三视口，无写按钮或配置 PATCH。 */
    async executeAction() {
      session = await loginPage(page, state, role, "routing-policies");
      // developer 快照始终绑定其只读 Session owner，不能复用 admin 的认证上下文。
      const scan = (label) => scanBrowserCheckpoint(page, evidence, state.secretRegistry, {
        activeToken: session.token,
        expectedOwner: session.owner,
        label: `${role}:${label}`,
      });
      await waitForCacheConfigRow(page, state, false);
      assert.equal(await cacheConfigRow(page, state).locator("button,input,select,textarea").count(), 0);
      const page073 = { states: {}, evidence };
      const captures = await captureReaderPages(page, state, role, page073, scan);
      assert.equal(
        evidence.targetRequests.filter((item) => item.surface === "cache_config_write").length,
        0,
      );
      const privacy = await scan("final");
      const target = assertRoleTargetEvidence(role, evidence);
      return {
        role,
        page072: { configuredReadonly: true, editorDomAbsent: true },
        page073: { states: page073.states },
        layouts: captures.layouts,
        screenshots: captures.screenshots,
        screenshotCount: captures.screenshots.length,
        privacy,
        privacyCheckpointCount: evidence.checkpointCount,
        bodyPrivacyScanCount: evidence.bodyPrivacyScanCount,
        externalRequestCount: evidence.blockedRequestCount,
        browserErrorCount: evidence.browserErrors.length,
        ...target,
      };
    },
    /** developer 登录后同样必须真实注销，不能依赖 Context 丢弃 Token。 */
    async logoutAction() {
      if (!session) return { completed: true, required: false, attemptCount: 0, responseStatuses: [], authStorageCleared: true };
      return { required: true, ...await logoutStrict(page, state, evidence, session) };
    },
    /** 关闭独立 Context，确保角色 Storage 完全隔离。 */
    async closeAction() { await context.close(); },
    /** 只读角色同样在 close 后终检网络、响应扫描和浏览器异常，不能依赖管理员阶段代检。 */
    finalEvidenceAction: () => assertBrowserGuardEvidence(evidence),
  });
}

/** viewer 直达 PAGE-072/073 均回退 overview，并保持导航、DOM 和目标 API 预取为零。 */
async function runViewerRole(browser, state) {
  const role = "viewer";
  const context = await browser.newContext({
    viewport: VIEWPORTS[0],
    userAgent: `prd050-browser-ui-${role}-${state.browserSuffix}`,
  });
  const page = await context.newPage();
  const evidence = await installBrowserGuards(context, page, state, role);
  let session = null;
  return runRoleLifecycle({
    stagePrefix: role,
    routeSupervisor: evidence.routeSupervisor,
    /** 对两个受限 hash 逐一攻击，核对 overview 回退、零 DOM 与零目标请求。 */
    async executeAction() {
      session = await loginPage(page, state, role, "routing-policies");
      // viewer 快照虽然没有目标业务 DOM，仍需精确核对当前 Session owner 与 URL/Storage。
      const scan = (label) => scanBrowserCheckpoint(page, evidence, state.secretRegistry, {
        activeToken: session.token,
        expectedOwner: session.owner,
        label: `${role}:${label}`,
      });
      const fallbacks = [];
      for (const navigation of ["routing-policies", "request-logs"]) {
        await page.evaluate((hash) => { window.location.hash = hash; }, navigation);
        await page.locator('.console-shell[data-active-navigation="overview"]').waitFor({
          state: "visible",
          timeout: 30_000,
        });
        await page.waitForTimeout(200);
        assert.equal(await page.locator(`[data-console-nav="${navigation}"]`).count(), 0);
        assert.equal(await page.locator(".routing-policies-panel, .request-logs-panel").count(), 0);
        assert.equal(await page.locator("[data-cache-affinity-row], .cache-affinity-selection-section").count(), 0);
        await scan(`fallback:${navigation}`);
        fallbacks.push({ requested: navigation, active: "overview", restrictedDom: false });
      }
      const screenshots = [];
      const layouts = {};
      for (const viewport of VIEWPORTS) {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        layouts[viewport.name] = await assertResponsiveLayout(page, viewport, "main");
        screenshots.push(await capturePage(page, state, role, "overview-fallback", viewport, scan));
      }
      const privacy = await scan("final");
      const target = assertRoleTargetEvidence(role, evidence);
      return {
        role,
        fallbacks,
        layouts,
        screenshots,
        screenshotCount: screenshots.length,
        privacy,
        privacyCheckpointCount: evidence.checkpointCount,
        bodyPrivacyScanCount: evidence.bodyPrivacyScanCount,
        externalRequestCount: evidence.blockedRequestCount,
        browserErrorCount: evidence.browserErrors.length,
        ...target,
      };
    },
    /** viewer 虽无业务预取，仍持有认证 Session，因此必须真实注销。 */
    async logoutAction() {
      if (!session) return { completed: true, required: false, attemptCount: 0, responseStatuses: [], authStorageCleared: true };
      return { required: true, ...await logoutStrict(page, state, evidence, session) };
    },
    /** 关闭 viewer 独立 Context，禁止残留认证 Storage。 */
    async closeAction() { await context.close(); },
    /** viewer 零业务请求不等于零浏览器错误，Context close 后仍需执行完整非 DOM 证据门禁。 */
    finalEvidenceAction: () => assertBrowserGuardEvidence(evidence),
  });
}

/**
 * 执行 Browser 主阶段并始终尝试关闭；主错与 browser.close 错误分别脱敏后聚合。
 * finally 直接 await close 会覆盖角色首错，因此关闭必须成为独立、可审计的第二阶段。
 */
async function runBrowserCloseLifecycle({ executeAction, closeAction }) {
  let result;
  const failures = [];
  try {
    result = await executeAction();
  } catch (error) {
    failures.push(toSafeStageError(error, "browser.roles", "role_execution_failed"));
  }
  try {
    await closeAction();
  } catch (error) {
    failures.push(toSafeStageError(error, "browser.close", "browser_close_failed"));
  }
  // 角色与 Browser close 任一失败都阻止结果返回，且双错不会互相覆盖。
  if (failures.length) throw new AggregateError(failures, "浏览器主生命周期失败");
  return result;
}

/** 启动真实 Chrome 并顺序执行三个隔离 Context；配置写状态仅由 admin 驱动。 */
async function runBrowserAcceptance(state) {
  const { chromium } = require("playwright");
  const browser = await chromium.launch({
    executablePath: state.args.chromePath,
    headless: !state.args.headed,
  });
  return runBrowserCloseLifecycle({
    /** 三个角色保持串行，管理员写状态完全收敛后只读角色才可开始。 */
    async executeAction() {
      const admin = await runAdminRole(browser, state);
      const developer = await runDeveloperRole(browser, state);
      const viewer = await runViewerRole(browser, state);
      return { admin, developer, viewer };
    },
    /** Browser 关闭失败不能覆盖角色错误，外层 runner 仍会按进程 owner 强制回收。 */
    closeAction: () => browser.close(),
  });
}

/** 通过真实 API 把 CacheAffinity 四字段恢复为运行验收前语义；版本推进保留审计真实性。 */
async function restoreOriginalConfig(state) {
  const token = state.apiTokens.get("admin");
  if (!token) return { required: false, restored: true, apiMutationCount: 0 };
  const current = await readCurrentConfig(state, token, "cleanup");
  if (configSemanticsEqual(current, state.facts.originalConfig)) {
    return { required: true, restored: true, apiMutationCount: 0 };
  }
  const restored = await patchConfigApi(
    state,
    token,
    configSemantics(state.facts.originalConfig),
    current.version,
    "cleanup_restore_config",
  );
  const verified = await readCurrentConfig(state, token, "cleanup_verify");
  assert.equal(configSemanticsEqual(verified, state.facts.originalConfig), true);
  assert.ok(restored.version > current.version);
  return { required: true, restored: true, apiMutationCount: 1 };
}

/** 逐枚调用真实注销 API；失败仍继续 SQLite owner 清理，但会阻止验收 passed。 */
async function logoutApiTokens(state) {
  let logoutCount = 0;
  const failures = [];
  for (const [role, token] of state.apiTokens) {
    try {
      const requestId = operationRequestId(`cleanup_logout_${role}`, state.browserSuffix);
      const { body } = await controlJson(state, "/auth/logout", {
        method: "POST",
        headers: authHeaders(token, requestId),
      }, 200);
      assert.equal(body.logged_out, true);
      logoutCount += 1;
    } catch {
      failures.push(role);
    }
  }
  return { logoutCount, failures };
}

/**
 * 在同批次 SQLite 内按 tenant+user/session owner 撤销临时身份。
 * 只更新本轮随机账号和精确 Session，不删除或合成任何 RequestLog/Selection 事实。
 */
function cleanupTemporaryAccounts(state) {
  const database = new DatabaseSync(state.databasePath);
  database.exec("PRAGMA foreign_keys=ON");
  database.exec("PRAGMA busy_timeout=10000");
  database.exec("BEGIN IMMEDIATE");
  let revokedSessionCount = 0;
  let disabledTemporaryUserCount = 0;
  try {
    const ownedUserIds = new Set(state.temporaryUserOwners.keys());
    for (const [role, intent] of state.userIntents) {
      const rows = database.prepare(`
        SELECT user_id, tenant_id, email, display_name, role, status, created_at
        FROM users WHERE tenant_id=? AND email=?
      `).all(state.runtime.tenantId, intent.email);
      assert.ok(rows.length <= 1);
      if (!rows.length) continue;
      const row = rows[0];
      assert.ok(
        row.tenant_id === state.runtime.tenantId
          && row.email === intent.email
          && row.display_name === intent.displayName
          && row.role === role,
      );
      if (!ownedUserIds.has(String(row.user_id))) {
        assert.ok(Date.parse(String(row.created_at)) >= state.startedAtMillis - 2000);
      }
      ownedUserIds.add(String(row.user_id));
    }
    for (const userId of ownedUserIds) {
      const sessions = database.prepare(`
        UPDATE sessions
        SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
        WHERE tenant_id=? AND user_id=? AND status!='revoked'
      `).run(state.cleanupTime, state.cleanupTime, state.runtime.tenantId, userId);
      assert.ok(Number(sessions.changes) <= 16);
      revokedSessionCount += Number(sessions.changes);
      const users = database.prepare(`
        UPDATE users SET status='disabled', updated_at=?
        WHERE tenant_id=? AND user_id=? AND status!='disabled'
      `).run(state.cleanupTime, state.runtime.tenantId, userId);
      assert.ok(Number(users.changes) <= 1);
      disabledTemporaryUserCount += Number(users.changes);
    }
    for (const [sessionId, owner] of state.sessionOwners) {
      const row = database.prepare(`
        SELECT tenant_id, user_id FROM sessions WHERE tenant_id=? AND session_id=?
      `).get(owner.tenantId, sessionId);
      assert.ok(row && row.user_id === owner.userId);
      const update = database.prepare(`
        UPDATE sessions
        SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
        WHERE tenant_id=? AND session_id=? AND status!='revoked'
      `).run(state.cleanupTime, state.cleanupTime, owner.tenantId, sessionId);
      assert.ok(Number(update.changes) <= 1);
      revokedSessionCount += Number(update.changes);
    }
    for (const [marker, owner] of state.sessionIntents) {
      const rows = database.prepare(`
        SELECT session_id, tenant_id, user_id, created_at
        FROM sessions
        WHERE tenant_id=? AND user_id=? AND user_agent=?
      `).all(owner.tenantId, owner.userId, marker);
      assert.ok(rows.length <= 1);
      for (const row of rows) {
        assert.ok(Date.parse(String(row.created_at)) >= state.startedAtMillis - 2000);
        const update = database.prepare(`
          UPDATE sessions
          SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
          WHERE tenant_id=? AND session_id=? AND user_id=? AND status!='revoked'
        `).run(state.cleanupTime, state.cleanupTime, owner.tenantId, row.session_id, owner.userId);
        assert.ok(Number(update.changes) <= 1);
        revokedSessionCount += Number(update.changes);
      }
    }
    let terminalEmptyAliasCount = 0;
    if (state.emptyAliasIntent) {
      const aliases = database.prepare(`
        SELECT a.model_alias_id, a.tenant_id, a.alias, a.environment_id, a.default_model_id,
               a.status, a.created_by, a.created_at,
               (SELECT COUNT(*) FROM cache_affinities ca
                WHERE ca.tenant_id=a.tenant_id AND ca.model_alias_id=a.model_alias_id) AS affinity_count
        FROM model_aliases a WHERE a.tenant_id=? AND a.alias=?
      `).all(state.emptyAliasIntent.tenantId, state.emptyAliasIntent.alias);
      assert.ok(aliases.length <= 1);
      if (aliases.length) {
        const alias = aliases[0];
        assert.ok(
          alias.tenant_id === state.emptyAliasIntent.tenantId
            && alias.alias === state.emptyAliasIntent.alias
            && alias.environment_id === state.emptyAliasIntent.environmentId
            && alias.default_model_id === state.emptyAliasIntent.modelId
            && alias.created_by === state.emptyAliasIntent.createdBy
            && alias.status === "disabled"
            && Number(alias.affinity_count) === 0
            && Date.parse(String(alias.created_at)) >= state.startedAtMillis - 2000,
        );
        // 响应已登记稳定 ID 时，cleanup 查询结果必须仍属于同一随机 Alias owner。
        if (state.emptyAliasOwner) assert.equal(alias.model_alias_id, state.emptyAliasOwner.modelAliasId);
        terminalEmptyAliasCount = 1;
      }
    }
    const activeUsers = Number(database.prepare(`
      SELECT COUNT(*) AS total FROM users
      WHERE tenant_id=? AND user_id IN (
        SELECT user_id FROM users WHERE tenant_id=? AND email LIKE ?
      ) AND status!='disabled'
    `).get(
      state.runtime.tenantId,
      state.runtime.tenantId,
      `prd050-browser-%-${state.browserSuffix}@metahub.local`,
    ).total);
    assert.equal(activeUsers, 0);
    assert.deepEqual(database.prepare("PRAGMA foreign_key_check").all(), []);
    assert.equal(Object.values(database.prepare("PRAGMA integrity_check").get())[0], "ok");
    database.exec("COMMIT");
    return {
      verified: true,
      revokedSessionCount,
      disabledTemporaryUserCount,
      terminalEmptyAliasCount,
      requestFactsUntouched: true,
      foreignKeyCheckClean: true,
      integrityCheckOk: true,
    };
  } catch (error) {
    database.exec("ROLLBACK");
    // 事务已回滚，向外抛出后由 cleanup 阶段 wrapper 脱敏并继续其他收口步骤。
    throw error;
  } finally {
    database.close();
  }
}

/** 无论主流程成功或失败都执行 cleanup；多个固定阶段失败聚合后阻止结果落盘。 */
async function runOwnedStateLifecycle({ executeAction, cleanupAction }) {
  let result;
  let cleanup;
  const failures = [];
  try {
    result = await executeAction();
  } catch (error) {
    failures.push(toSafeStageError(error, "browser.execute", "execution_failed"));
  }
  try {
    cleanup = await cleanupAction();
  } catch (error) {
    failures.push(toSafeStageError(error, "browser.cleanup", "cleanup_failed"));
  }
  // execute 与 cleanup 失败一并聚合，最终制品只允许在两者均成功时写入。
  if (failures.length) throw new AggregateError(failures, "PRD-050 浏览器验收生命周期失败");
  return { result, cleanup };
}

/** 顺序执行所有 cleanup 阶段并聚合脱敏失败；前一阶段失败不得阻止后续 owner 收口。 */
async function runBestEffortCleanupStages(stages) {
  const results = {};
  const failures = [];
  for (const [name, action] of stages) {
    assert.ok(SAFE_CODE_PATTERN.test(String(name)));
    try {
      results[name] = await action();
    } catch (error) {
      failures.push(toSafeStageError(error, `browser.cleanup.${name}`, "cleanup_stage_failed"));
    }
  }
  // 全部阶段已经逐项尝试；此处只聚合固定阶段错误并阻止 passed 结果落盘。
  if (failures.length) throw new AggregateError(failures, "PRD-050 cleanup 阶段失败");
  return results;
}

/** 恢复配置、注销 API Session 并在 SQLite 中精确禁用临时账号；四阶段始终全部尝试。 */
async function cleanupAcceptance(state) {
  const stages = await runBestEffortCleanupStages([
    ["config", () => restoreOriginalConfig(state)],
    ["api_logout", async () => {
      const result = await logoutApiTokens(state);
      assert.deepEqual(result.failures, []);
      return result;
    }],
    ["database", async () => {
      state.cleanupTime = new Date().toISOString();
      return cleanupTemporaryAccounts(state);
    }],
    ["ledger", async () => {
      assert.ok(state.mutationLedger.every((item) => item.outcome !== "pending"));
      return { mutationCount: state.mutationLedger.length };
    }],
  ]);
  return {
    configRestored: stages.config.restored,
    configRestoreMutationCount: stages.config.apiMutationCount,
    apiLogoutCount: stages.api_logout.logoutCount,
    mutationCount: stages.ledger.mutationCount,
    ...stages.database,
  };
}

/** 递归拒绝结果键中可能承载内部 ID/分类/正文的字段，并扫描所有字符串值。 */
function assertResultRedacted(result, exactSecrets = []) {
  const forbiddenKeys = new Set([
    "password",
    "access_token",
    "provider_credential_id",
    "cache_affinity_binding_id",
    "cache_affinity_scope_id",
    "data_class",
    "key_prefix",
    "prompt_fingerprint",
    "endpoint_revision_digest",
  ]);
  // 递归只检查键和值是否可公开，不把遍历中的任何敏感原文复制到错误或结果对象。
  const visit = (value) => {
    if (typeof value === "string") {
      assert.equal(containsSecret(value, exactSecrets), false);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!value || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      assert.ok(!forbiddenKeys.has(key.toLowerCase()));
      visit(child);
    }
  };
  visit(result);
  return true;
}

/** 构造只驻内存的 owner、mutation 和 Secret 账本；稳定结果不引用这些 Map/Set。 */
function createAcceptanceState(args, runtime, databasePath, facts) {
  const browserSuffix = randomBytes(6).toString("hex");
  return {
    args,
    origins: {
      controlOrigin: args.controlOrigin,
      gatewayOrigin: args.gatewayOrigin,
      consoleOrigin: args.consoleOrigin,
    },
    runtime,
    databasePath,
    facts,
    browserSuffix,
    startedAtMillis: Date.now(),
    cleanupTime: "",
    uiLoginSequence: 0,
    mutationSequence: 0,
    mutationLedger: [],
    screenshotHashes: new Set(),
    secretRegistry: collectConfiguredSecrets([args.adminPassword]),
    forbiddenUrlValues: [facts.originalConfig.data_class].filter((value) => String(value ?? "").length),
    emptyAliasOwner: null,
    accounts: { admin: { email: args.adminEmail, password: args.adminPassword } },
    apiTokens: new Map(),
    sessionOwners: new Map(),
    sessionIntents: new Map(),
    userIntents: new Map(),
    temporaryUserOwners: new Map(),
    emptyAliasIntent: null,
  };
}

/** 验证文件、Chrome 和同批次数据库边界；本函数不启动服务或修改数据库。 */
function assertLocalScope(args, databasePath) {
  assert.ok(statSync(args.chromePath).isFile());
  assert.ok(statSync(args.runtimeStatePath).isFile());
  assert.ok(statSync(databasePath).isFile());
  assert.ok(pathIsInside(args.batchDirectory, databasePath));
  assert.ok(pathIsInside(args.batchDirectory, args.browserDirectory));
  assert.ok(pathIsInside(args.browserDirectory, args.resultPath));
  assertPhysicalPathInside(args.artifactRoot, args.batchDirectory);
  for (const target of [
    args.runtimeStatePath,
    databasePath,
    args.browserDirectory,
    args.resultPath,
  ]) {
    assertPhysicalPathInside(args.batchDirectory, target);
  }
  return true;
}

/**
 * 独占创建本批次的 Browser 证据目录。
 *
 * recursive:false 是重入门禁：目录、旧结果或任一旧 PNG 已存在时，整次验收必须在写新证据前失败，
 * 不能复用目录后形成新旧截图混合的伪闭集。
 */
async function createBrowserEvidenceDirectory(browserDirectory) {
  await mkdir(browserDirectory, { recursive: false });
  return browserDirectory;
}

/**
 * 独占写入单份 Browser 证据。
 *
 * @param {string} targetPath 同批次 browser 目录内的目标文件。
 * @param {string|Buffer|Uint8Array} payload 已完成隐私扫描的结果或 PNG 字节。
 * @returns {Promise<string>} 成功创建的目标路径。
 */
async function writeNewEvidenceFile(targetPath, payload) {
  await writeFile(targetPath, payload, { flag: "wx" });
  return targetPath;
}

/** 为最终结果生成同目录随机临时路径，避免 PID 重用或并发进程共享临时文件名。 */
function createResultTemporaryPath(resultPath) {
  const nonce = randomBytes(18).toString("hex");
  return path.join(
    path.dirname(resultPath),
    `.${path.basename(resultPath)}.${process.pid}.${nonce}.tmp`,
  );
}

/**
 * 以 write-once 语义发布最终 JSON：临时文件独占创建并刷盘，再用 hard link 原子创建目标。
 * 只有本调用成功 open("wx") 的临时文件才会清理；若同名临时文件属于其他进程，绝不删除其字节。
 */
async function publishWriteOnceResult(
  resultPath,
  encoded,
  { temporaryPathFactory = createResultTemporaryPath } = {},
) {
  const resolvedResultPath = path.resolve(resultPath);
  const temporaryPath = path.resolve(temporaryPathFactory(resolvedResultPath));
  const temporaryName = path.basename(temporaryPath);
  const expectedPrefix = `.${path.basename(resolvedResultPath)}.${process.pid}.`;
  assert.equal(path.dirname(temporaryPath), path.dirname(resolvedResultPath), "结果临时文件必须与目标位于同一目录");
  assert.ok(
    temporaryName.startsWith(expectedPrefix) && temporaryName.endsWith(".tmp"),
    "结果临时文件名不属于当前 Browser 进程",
  );

  let ownsTemporaryPath = false;
  try {
    const handle = await open(temporaryPath, "wx");
    ownsTemporaryPath = true;
    try {
      await handle.writeFile(encoded, { encoding: "utf8" });
      // hard link 发布前必须把完整 JSON 刷到磁盘，避免 closeout 观察到仅存在但内容未落盘的结果。
      await handle.sync();
      await link(temporaryPath, resolvedResultPath);
    } finally {
      await handle.close();
    }
  } finally {
    if (ownsTemporaryPath) {
      try {
        await unlink(temporaryPath);
      } catch (error) {
        // 临时文件已不存在等价于清理完成；其他清理失败必须让验收失败，不能留下未审计制品。
        if (error?.code !== "ENOENT") throw error;
      }
    }
  }
  return resolvedResultPath;
}

/** 仅在完整页面、权限、隐私、cleanup 均通过后以 write-once 语义写入脱敏结果。 */
async function main() {
  const args = parseBrowserArgs();
  const runtime = await loadRuntimeState(args.runtimeStatePath);
  assert.equal(runtime.generation, args.generation);
  const databasePath = resolveRuntimeDatabasePath(args.batchDirectory, runtime.databaseFile);
  assertLocalScope(args, databasePath);
  const facts = loadStableDatabaseFacts(databasePath, runtime, args.adminEmail);
  const state = createAcceptanceState(args, runtime, databasePath, facts);
  await createBrowserEvidenceDirectory(args.browserDirectory);

  const lifecycle = await runOwnedStateLifecycle({
    /** 主流程仅连接调用方已启动的服务，并使用真实 API/Chrome。 */
    async executeAction() {
      const services = await runAcceptanceStage("services.verify", () => verifyServices(state));
      const accounts = await runAcceptanceStage("accounts.prepare", () => prepareAccounts(state));
      const backendRbac = await runAcceptanceStage("backend_rbac.verify", () => verifyBackendPermissions(state));
      const roles = await runAcceptanceStage("browser.roles", () => runBrowserAcceptance(state));
      return { services, accounts, backendRbac, roles };
    },
    /** cleanup 必须在主流程任何失败后继续执行。 */
    cleanupAction: () => cleanupAcceptance(state),
  });
  const { admin, developer, viewer } = lifecycle.result.roles;
  const result = {
    schemaVersion: 1,
    status: "passed",
    prdId: "PRD-050",
    generation: args.generation,
    runtimeSuffix: runtime.suffix,
    services: lifecycle.result.services,
    accounts: lifecycle.result.accounts,
    backendRbac: lifecycle.result.backendRbac,
    admin,
    developer,
    viewer,
    viewportCount: VIEWPORTS.length,
    screenshotCount: admin.screenshotCount + developer.screenshotCount + viewer.screenshotCount,
    cleanup: lifecycle.cleanup,
  };
  assertResultRedacted(result, state.secretRegistry);
  const encoded = `${JSON.stringify(result, null, 2)}\n`;
  await publishWriteOnceResult(args.resultPath, encoded);


  process.stdout.write(encoded);
  state.screenshotHashes.clear();
  state.secretRegistry.clear();
  state.apiTokens.clear();
  state.accounts = {};
}

// 单元测试 import 时只暴露纯/helper 合同；直接执行才连接数据库、服务和 Chrome。
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    await main();
  } catch (error) {
    const safe = toSafeStageError(error, "browser.main", "acceptance_failed");
    process.stderr.write(`${safe.message}\n`);
    process.exitCode = 1;
  }
}

export {
  BrowserAcceptanceStageError,
  GUARDED_ROUTE_FETCH_OPTIONS,
  NAVIGATION_TARGET_SELECTORS,
  PAGE073_EXPECTATIONS,
  REQUIRED_RUNTIME_REQUEST_KEYS,
  REQUIRED_VIEWPORT_STATES,
  SAFE_SELECTION_FIELDS,
  VIEWPORTS,
  assertBackendPermissionEvidence,
  assertBrowserGuardEvidence,
  assertGuardedRouteFetchRequest,
  backendPermissionExpectation,
  assertCacheEvidenceIndependent,
  assertConflictRefreshEvidence,
  assertLayoutMetrics,
  assertPage073DetailPayload,
  assertPage072LoadingEvidence,
  assertPhysicalPathInside,
  assertRetryCancelEvidence,
  assertResultRedacted,
  assertRoleTargetEvidence,
  assertRuntimeStateKeyPrivacy,
  assertSensitiveSurfaceSnapshot,
  assertSessionSwitchEvidence,
  assertTransportConsoleConverged,
  assertUrlPrivacy,
  assertViewportStateMatrix,
  beginExpectedTransportConsoleError,
  classifyTargetApi,
  classifyUnexpectedBrowserConsoleError,
  collectConfiguredSecrets,
  configSemantics,
  configSemanticsEqual,
  createBrowserEvidenceDirectory,
  containsSecret,
  createBrowserRouteFailureSupervisor,
  createDeferred,
  createResultTemporaryPath,
  decideBrowserNetworkRequest,
  executeLogoutWithRetry,
  fallThroughBrowserRoute,
  hasSingleExactResponseHeader,
  installAfterCommitPatchGate,
  installBeforeSendPatchGate,
  recordBrowserConsoleError,
  normalizeLoopbackOrigin,
  observePromiseOutcome,
  parseBrowserArgs,
  pathIsInside,
  projectTargetResponseBody,
  publishWriteOnceResult,
  resolveRuntimeDatabasePath,
  resolveAcceptanceRoots,
  runAcceptanceStage,
  runBestEffortCleanupStages,
  runBrowserActionWithObservedResponse,
  runBrowserCloseLifecycle,
  runOwnedStateLifecycle,
  runRoleLifecycle,
  scheduleTargetResponseScan,
  screenshotRoleStyle,
  settleExpectedTransportConsoleError,
  settleBrowserRouteBeforeUnroute,
  settleMutation,
  superviseBrowserRoute,
  targetCorsOriginValid,
  targetRequestCorsOriginValid,
  targetPreflightHeadersValid,
  toSafeStageError,
  validateRuntimeStateDocument,
  writeNewEvidenceFile,
};
