/**
 * PRD-046 策略模拟器真实 Chrome 验收。
 *
 * 脚本只允许访问回环服务和工作区内 SQLite；业务准备与恢复优先走公开 API。
 * 密码、Token、Prompt sentinel 和完整响应只驻留当前进程内存，结构化输出仅包含
 * 状态、计数和布尔证据。用户禁用是当前公开 API 的唯一缺口，因此最终回收会在
 * 精确 tenant + user ID 白名单内撤销 Session 并禁用一次性账号。
 */

import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const require = createRequire(import.meta.url);
// Playwright 由工作区验收运行时提供，不能为了验收脚本改写 Console 生产依赖。
const { chromium } = require("playwright");

// 服务地址、租户身份、受控数据库、截图目录和 Chrome 路径共同限定真实验收边界。
const CONTROL_API_URL = process.env.METAHUB_CONTROL_API_URL ?? "http://localhost:8001";
const CONSOLE_ROOT_URL = process.env.METAHUB_CONSOLE_URL ?? "http://localhost:5173";
const TENANT_SLUG = (process.env.METAHUB_TENANT_SLUG ?? "default").trim();
const ADMIN_EMAIL = (process.env.METAHUB_ADMIN_EMAIL ?? "admin@metahub.local").trim().toLowerCase();
const DATABASE_PATH = path.resolve(
  process.env.METAHUB_DATABASE_PATH ?? ".data/metahub-dev-prd031.db",
);
const ARTIFACT_DIR = path.resolve(
  process.env.METAHUB_PRD046_ARTIFACT_DIR ?? "artifacts/prd046",
);
const CHROME_PATH = process.env.METAHUB_CHROME_PATH
  ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const POLICY_SIMULATOR_URL = new URL("#policy-simulator", `${CONSOLE_ROOT_URL}/`).toString();

// 这些表代表真实 Gateway 调用、路由、用量、DLP 和预算事实；Dry-Run 前后必须逐表不变。
const ZERO_COST_TABLES = Object.freeze([
  "gateway_request_claims",
  "request_logs",
  "usage_ledger",
  "price_snapshots",
  "routing_decisions",
  "routing_attempts",
  "dlp_matches",
  "budget_evaluations",
  "budget_reservations",
  "budget_blocks",
  "budget_block_events",
  "budget_alert_events",
]);
// Dry-Run 事件只允许这些固定安全摘要字段，禁止把 Prompt、模型输入、DSL 或预算余额带入 Webhook。
const DRY_RUN_EVENT_FIELDS = Object.freeze([
  "budget_status",
  "checked_policy_count",
  "coverage_complete",
  "decision",
  "decision_reason_code",
  "dlp_status",
  "matched_policy_id",
  "policy_explanation_id",
  "policy_set_fingerprint",
  "request_id",
  "route_status",
  "runtime_compatible",
  "would_error_code",
]);
// 三档视口同时声明期望表单列数，避免只检查 scrollWidth 却漏掉响应式规则未生效。
const VIEWPORTS = Object.freeze([
  { name: "desktop-1280x720", width: 1280, height: 720, formColumns: 2 },
  { name: "intermediate-900x800", width: 900, height: 800, formColumns: 1 },
  { name: "mobile-390x844", width: 390, height: 844, formColumns: 1 },
]);

/**
 * 读取必需环境变量。
 *
 * 参数：name 为变量名。返回：去除首尾空白后的值。
 * 安全：失败只报告变量名，不打印环境内容。
 */
function requiredEnvironment(name) {
  const value = String(process.env[name] ?? "").trim();
  assert.ok(value, `缺少必需环境变量 ${name}`);
  return value;
}

/**
 * 确认验收不会修改远端服务或工作区外数据库。
 *
 * 约束：必须显式确认；Control/Console 只能是回环主机；数据库和证据目录必须在工作区内。
 */
function assertLocalScope() {
  assert.equal(process.env.METAHUB_CONFIRM_BROWSER_STATE_MUTATION, "1");
  const allowedHosts = new Set(["127.0.0.1", "::1", "localhost"]);
  for (const value of [CONTROL_API_URL, CONSOLE_ROOT_URL]) {
    assert.ok(allowedHosts.has(new URL(value).hostname), "浏览器验收只允许回环地址");
  }
  assert.ok(TENANT_SLUG && ADMIN_EMAIL, "租户和管理员标识不能为空");
  const workspace = path.resolve(".");
  for (const [label, localPath] of [["数据库", DATABASE_PATH], ["证据目录", ARTIFACT_DIR]]) {
    assert.ok(localPath.startsWith(`${workspace}${path.sep}`), `${label}必须位于工作区内`);
  }
}

/**
 * 生成 Bearer Header，并按需附带稳定 request_id。
 *
 * 参数：只驻留内存的 Token 和可选 requestId。返回：供单次 fetch 使用的 Header。
 */
function authHeaders(token, requestId = null) {
  const headers = { Authorization: `Bearer ${token}` };
  if (requestId) headers["X-Request-ID"] = requestId;
  return headers;
}

/**
 * 调用 Control JSON API 并校验 HTTP、统一链路 ID 和可选错误码。
 *
 * 安全：断言失败只输出路径、HTTP 与固定错误码，不复制响应正文。
 */
async function controlJson(pathname, options, expectedStatus, expectedCode = null) {
  const response = await fetch(`${CONTROL_API_URL}${pathname}`, options);
  let body = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  assert.equal(
    response.status,
    expectedStatus,
    `${pathname} HTTP ${response.status}, code=${body.code ?? "-"}`,
  );
  if (expectedCode !== null) assert.equal(body.code, expectedCode);
  assert.ok(body.request_id, `${pathname} 缺少 request_id`);
  assert.ok(body.trace_id, `${pathname} 缺少 trace_id`);
  return { body, response };
}

/**
 * 使用公开认证接口登录。
 *
 * 参数：邮箱、内存密码和唯一 User-Agent。返回：Token、用户与 Session 摘要。
 */
async function apiLogin(email, password, sessionMarker) {
  const { body } = await controlJson(
    "/auth/login",
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "User-Agent": sessionMarker },
      body: JSON.stringify({ tenant_slug: TENANT_SLUG, email, password }),
    },
    200,
  );
  assert.ok(body.access_token && body.user?.user_id && body.user?.tenant_id);
  return body;
}

/**
 * 创建逐项登记的验收账本。
 *
 * 账本不进入最终输出；其中密码、Token 和 sentinel 只用于当前进程内的准备、验证与清理。
 */
function createStateLedger() {
  const suffix = randomBytes(5).toString("hex");
  return {
    phase: "initialization",
    // 失败诊断只暴露固定阶段名，避免为定位浏览器断言而输出 Prompt、Token 或响应正文。
    checkpoint: "state_created",
    suffix,
    promptSentinel: `PRD046_PROMPT_SENTINEL_${suffix}`,
    promptHash: null,
    dataClass: `prd046_acceptance_${suffix}`,
    tenantId: null,
    bootstrapAdminToken: null,
    bootstrapAdminUserId: null,
    bootstrapSessionId: null,
    bootstrapSessionMarker: `prd046-browser-bootstrap-${suffix}`,
    accounts: {},
    roleTokens: {},
    userIntents: [],
    userIds: [],
    userIdsByRole: {},
    tempProjectIntent: {
      projectCode: `prd046-browser-${suffix}`,
      name: `PRD046 browser ${suffix}`,
    },
    tempProject: null,
    originalActivePolicies: [],
    tempPolicyIntent: {
      name: `PRD046 browser acceptance ${suffix}`,
      description: "PRD-046 browser acceptance policy",
    },
    tempPolicy: null,
    context: null,
  };
}

/**
 * 用公开 API 创建三种临时角色、Project、隔离候选并创建确定性 allow Policy。
 *
 * 关键约束：每个写入意图在调用前登记；原 active Policy 的 ID/版本逐项保存，失败清理也能精确恢复。
 */
async function prepareState(state) {
  state.phase = "bootstrap_login";
  state.promptHash = createHash("sha256").update(state.promptSentinel).digest("hex");
  const adminPassword = requiredEnvironment("METAHUB_ADMIN_PASSWORD");
  const bootstrap = await apiLogin(ADMIN_EMAIL, adminPassword, state.bootstrapSessionMarker);
  state.bootstrapAdminToken = bootstrap.access_token;
  state.bootstrapAdminUserId = bootstrap.user.user_id;
  state.bootstrapSessionId = String(bootstrap.session_id);
  state.tenantId = bootstrap.user.tenant_id;

  state.phase = "temporary_users";
  for (const role of ["admin", "developer", "viewer"]) {
    const account = {
      email: `prd046-browser-${role}-${state.suffix}@metahub.local`,
      password: `Prd046!${randomBytes(20).toString("base64url")}`,
    };
    const displayName = `PRD046 browser ${role}`;
    state.accounts[role] = account;
    state.userIntents.push({ email: account.email, displayName, role });
    const { body: created } = await controlJson(
      "/users",
      {
        method: "POST",
        headers: {
          ...authHeaders(state.bootstrapAdminToken, `req_prd046_browser_user_${role}_${state.suffix}`),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          email: account.email,
          display_name: displayName,
          password: account.password,
          role,
        }),
      },
      201,
    );
    state.userIds.push(created.user.user_id);
    state.userIdsByRole[role] = created.user.user_id;
    // 临时 admin 只通过真实 Console 登录；其他角色另取 Token 用于绕过前端的后端直攻。
    if (role !== "admin") {
      const login = await apiLogin(
        account.email,
        account.password,
        `prd046-browser-api-${role}-${state.suffix}`,
      );
      state.roleTokens[role] = login.access_token;
    }
  }

  state.phase = "temporary_project";
  const { body: projectBody } = await controlJson(
    "/projects",
    {
      method: "POST",
      headers: {
        ...authHeaders(state.bootstrapAdminToken, `req_prd046_browser_project_${state.suffix}`),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        project_code: state.tempProjectIntent.projectCode,
        name: state.tempProjectIntent.name,
        description: "PRD-046 browser acceptance",
      }),
    },
    201,
  );
  state.tempProject = projectBody.project;

  state.phase = "context_lookup";
  state.context = await locateDryRunContext(state);

  state.phase = "candidate_isolation";
  const { body: listed } = await controlJson(
    "/policies?status=active",
    { headers: authHeaders(state.bootstrapAdminToken) },
    200,
  );
  for (const policy of listed.policies ?? []) {
    const restore = { policyId: policy.policy_id, originalVersion: Number(policy.version) };
    state.originalActivePolicies.push(restore);
    const { body: disabled } = await controlJson(
      `/policies/${encodeURIComponent(policy.policy_id)}`,
      {
        method: "PATCH",
        headers: {
          ...authHeaders(state.bootstrapAdminToken, `req_prd046_isolate_${policy.policy_id}`),
          "Content-Type": "application/json",
          "If-Match": `"${policy.version}"`,
        },
        body: JSON.stringify({ status: "disabled" }),
      },
      200,
    );
    restore.disabledVersion = Number(disabled.policy.version);
  }

  state.phase = "temporary_policy";
  const { body: policyBody } = await controlJson(
    "/policies",
    {
      method: "POST",
      headers: {
        ...authHeaders(state.bootstrapAdminToken, `req_prd046_browser_policy_${state.suffix}`),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        name: state.tempPolicyIntent.name,
        description: state.tempPolicyIntent.description,
        status: "active",
        priority: 1,
        rule: { condition_expr: `data_class == "${state.dataClass}"` },
        action: { type: "allow" },
      }),
    },
    201,
  );
  state.tempPolicy = policyBody.policy;
  assert.equal(state.tempPolicy.status, "active");
  assert.equal(state.tempPolicy.action.type, "allow");
}

/**
 * 通过公开列表 API 定位 active Environment 和可解析模型，并绑定本轮临时 Project。
 *
 * 返回：页面下拉框与 Dry-Run DTO 共用的 environmentId/projectId/model。
 */
async function locateDryRunContext(state) {
  const headers = authHeaders(state.bootstrapAdminToken);
  const [{ body: environments }, { body: aliases }, { body: models }] = await Promise.all([
    controlJson("/environments?status=active", { headers }, 200),
    controlJson("/model-aliases?status=active", { headers }, 200),
    controlJson("/models?status=active", { headers }, 200),
  ]);
  const environment = (environments.environments ?? [])[0];
  assert.ok(environment?.environment_id, "租户没有 active Environment");
  const matchingAlias = (aliases.model_aliases ?? []).find((item) => (
    item.status === "active"
    && item.alias
    && (!item.environment_id || item.environment_id === environment.environment_id)
    && (!item.environment || item.environment === environment.code)
  ));
  const directModel = (models.models ?? []).find((item) => (
    item.status === "active"
    && (item.model_alias || item.internal_model_id || item.model_id)
  ));
  const model = matchingAlias?.alias
    ?? directModel?.model_alias
    ?? directModel?.internal_model_id
    ?? directModel?.model_id;
  assert.ok(model, "租户没有 active 可解析模型");
  return {
    environmentId: String(environment.environment_id),
    projectId: String(state.tempProject.project_id),
    model: String(model),
  };
}

/**
 * 收集浏览器错误和敏感正文泄露事实。
 *
 * 原始 console/pageerror 文本不进入输出；只记录错误类别和是否包含 sentinel/hash。
 */
function captureBrowserSignals(page, state) {
  const signals = { errors: [], sensitiveMessageFound: false };
  page.on("pageerror", (error) => {
    const text = String(error?.message ?? "");
    if (text.includes(state.promptSentinel) || text.includes(state.promptHash)) {
      signals.sensitiveMessageFound = true;
    }
    signals.errors.push("pageerror");
  });
  page.on("console", (message) => {
    const value = message.text();
    if (value.includes(state.promptSentinel) || value.includes(state.promptHash)) {
      signals.sensitiveMessageFound = true;
    }
    // error console 表示页面运行异常；原始消息可能含内部文本，因此只记录固定类别。
    if (message.type() === "error") signals.errors.push("console_error");
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      const url = new URL(response.url());
      signals.errors.push(`http_${response.status()}_${url.pathname}`);
    }
  });
  return signals;
}

/**
 * 给浏览器上下文安装回环网络硬门禁。
 *
 * Console 可访问 localhost 的 Gateway/Control/Vite，但任何远端 HTTP(S) 请求都会被中止并登记，
 * 防止验收中的 Prompt、身份或治理上下文意外流向第三方资源。
 */
async function installLoopbackNetworkGuard(context) {
  const violations = [];
  const allowedHosts = new Set(["127.0.0.1", "::1", "localhost"]);
  await context.route("**/*", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (["http:", "https:"].includes(requestUrl.protocol) && allowedHosts.has(requestUrl.hostname)) {
      await route.continue();
      return;
    }
    // 违规摘要只保留协议，不能把可能含敏感 query 的完整 URL 写入输出。
    violations.push(requestUrl.protocol);
    await route.abort("blockedbyclient");
  });
  return violations;
}

/**
 * 使用真实 Console 登录表单进入指定 hash。
 *
 * 返回：登录提交前浏览器实际请求的 hash，供越权直达回退断言。
 */
async function loginPage(page, account, expectedRole, targetUrl = POLICY_SIMULATOR_URL) {
  await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
  const requestedHash = await page.evaluate(() => window.location.hash);
  await page.locator("#login-form").waitFor({ state: "visible" });
  await page.locator('[name="tenantSlug"]').fill(TENANT_SLUG);
  await page.locator('[name="email"]').fill(account.email);
  await page.locator('[name="password"]').fill(account.password);
  await page.locator('#login-form button[type="submit"]').click();
  await page.locator(".console-shell").waitFor({ state: "visible", timeout: 30_000 });
  assert.match(await page.locator(".user-menu").innerText(), new RegExp(`\\b${expectedRole}\\b`));
  return { requestedHash };
}

/**
 * 等待 Console 异步刷新稳定。
 *
 * 周期刷新可能让 networkidle 超时，因此超时后仅增加一个短稳定窗口。
 */
async function waitForConsoleStability(page) {
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
  await page.waitForTimeout(500);
}

/**
 * 注销当前浏览器会话。
 *
 * 完成时必须重新显示登录表单，避免一次性 Session 遗留。
 */
async function logoutPage(page) {
  const button = page.locator("#logout-button");
  if (await button.count()) {
    await button.click();
    await page.locator("#login-form").waitFor({ state: "visible", timeout: 30_000 });
  }
}

/**
 * 在异常路径尽力注销浏览器会话。
 *
 * 页面或服务异常不能阻止 finally 中继续执行服务端精确清理。
 */
async function logoutPageBestEffort(page) {
  try {
    if (!page.isClosed()) await logoutPage(page);
  } catch {
    // 后续用户/Session 白名单清理仍会收口；这里不能用不可信异常覆盖首个验收失败。
  }
}

/**
 * 读取所有用户表名。
 *
 * 返回：不含 sqlite 内部表的稳定排序数组；标识符来自 sqlite_schema 而非外部输入。
 */
function databaseTables(database) {
  return database.prepare(`
    SELECT name FROM sqlite_schema
    WHERE type='table' AND name NOT LIKE 'sqlite_%'
    ORDER BY name
  `).all().map((row) => String(row.name));
}

/**
 * 安全引用 SQLite 标识符。
 *
 * 参数只允许来自 sqlite_schema 或脚本固定常量；双引号转义仍防止未来误用。
 */
function quotedIdentifier(value) {
  return `"${String(value).replaceAll('"', '""')}"`;
}

/**
 * 对指定表全部行生成不可逆摘要。
 *
 * 摘要用于发现插入和原行更新；原始 BLOB、Key、URL 或历史正文不会进入验收输出。
 */
function tableDigest(database, table) {
  const exists = database.prepare(
    "SELECT 1 AS present FROM sqlite_schema WHERE type='table' AND name=?",
  ).get(table);
  if (!exists) return { present: false, rows: 0, digest: null };
  const rows = database.prepare(`SELECT * FROM ${quotedIdentifier(table)} ORDER BY rowid`).all();
  const digest = createHash("sha256");
  for (const row of rows) {
    for (const [key, value] of Object.entries(row)) {
      digest.update(key);
      digest.update("=");
      if (value instanceof Uint8Array) digest.update(value);
      else digest.update(String(value));
      digest.update("\u0000");
    }
    digest.update("\n");
  }
  return { present: true, rows: rows.length, digest: digest.digest("hex") };
}

/**
 * 捕获真实调用成本相关表和 Provider 状态基线。
 *
 * 返回值只有行数和摘要，不暴露任何业务值。
 */
function snapshotZeroCostFacts() {
  const database = new DatabaseSync(DATABASE_PATH, { readOnly: true });
  try {
    const snapshot = {};
    for (const table of [...ZERO_COST_TABLES, "providers", "provider_credentials"]) {
      snapshot[table] = tableDigest(database, table);
    }
    return snapshot;
  } finally {
    database.close();
  }
}

/**
 * 对比 Dry-Run 前后的真实调用事实。
 *
 * 任一行数或摘要变化都失败；输出只保留通过表数。
 */
function assertZeroCostUnchanged(before, after) {
  for (const table of Object.keys(before)) {
    assert.equal(after[table].present, before[table].present, `${table} 存在性变化`);
    assert.equal(after[table].rows, before[table].rows, `${table} 行数变化`);
    assert.equal(after[table].digest, before[table].digest, `${table} 内容变化`);
  }
  return { unchangedTableCount: Object.keys(before).length, providerCalled: false };
}

/**
 * 扫描数据库全部文本列是否出现 Prompt sentinel 或其 SHA-256。
 *
 * 返回：命中行数；SQL 参数化且不把被扫描值写入异常或输出。
 */
function scanDatabaseSensitiveValues(database, state) {
  let hits = 0;
  for (const table of databaseTables(database)) {
    const columns = database.prepare(`PRAGMA table_info(${quotedIdentifier(table)})`).all()
      .filter((column) => String(column.type ?? "").toUpperCase().includes("TEXT"));
    for (const column of columns) {
      const sql = `SELECT COUNT(*) AS total FROM ${quotedIdentifier(table)} WHERE `
        + `instr(CAST(${quotedIdentifier(column.name)} AS TEXT), ?) > 0 `
        + `OR instr(CAST(${quotedIdentifier(column.name)} AS TEXT), ?) > 0`;
      hits += Number(database.prepare(sql).get(state.promptSentinel, state.promptHash).total);
    }
  }
  return hits;
}

/**
 * 权威回读 explanation/event/audit/Webhook 安全摘要并执行全库隐私扫描。
 *
 * 返回：事实行数和 Webhook 投递计数；不返回持久对象 ID 或 payload 正文。
 */
function verifyPersistentFacts(state, responseBody) {
  const requestId = responseBody.request_id;
  const explanation = responseBody.policy_explanation;
  const database = new DatabaseSync(DATABASE_PATH, { readOnly: true });
  try {
    // Dry-Run 必须归属真实 Console 登录的临时 admin，不能错记到用于准备资源的 bootstrap 管理员。
    const actorId = state.userIdsByRole.admin;
    const exactExplanationRows = database.prepare(`
      SELECT * FROM policy_explanations
      WHERE tenant_id=? AND actor_user_id=? AND request_id=?
    `).all(state.tenantId, actorId, requestId);
    assert.equal(exactExplanationRows.length, 1);
    const events = database.prepare(`
      SELECT * FROM policy_events
      WHERE tenant_id=? AND actor_user_id=? AND request_id=?
        AND event_type='policy.dry_run_executed'
    `).all(state.tenantId, actorId, requestId);
    const audits = database.prepare(`
      SELECT * FROM audit_logs
      WHERE tenant_id=? AND actor_user_id=? AND request_id=?
        AND action='policy.dry_run_executed'
    `).all(state.tenantId, actorId, requestId);
    assert.equal(events.length, 1);
    assert.equal(audits.length, 1);
    const persisted = exactExplanationRows[0];
    const event = events[0];
    const audit = audits[0];
    const eventPayload = JSON.parse(String(event.payload_json));
    const auditBefore = JSON.parse(String(audit.before_summary_json));
    const auditAfter = JSON.parse(String(audit.after_summary_json));
    assert.deepEqual(Object.keys(eventPayload).sort(), [...DRY_RUN_EVENT_FIELDS]);
    assert.deepEqual(auditBefore, {});
    assert.equal(persisted.policy_explanation_id, explanation.policy_explanation_id);
    assert.equal(event.reason_code, "policy.dry_run_executed");
    assert.equal(event.trace_id, responseBody.trace_id);
    assert.equal(audit.trace_id, responseBody.trace_id);
    assert.equal(audit.object_type, "policy_explanation");
    assert.equal(audit.object_id, explanation.policy_explanation_id);
    assert.equal(eventPayload.request_id, requestId);
    assert.equal(eventPayload.policy_explanation_id, explanation.policy_explanation_id);
    assert.equal(eventPayload.decision, explanation.policy.decision);
    assert.equal(eventPayload.matched_policy_id, state.tempPolicy.policy_id);
    assert.equal(auditAfter.policy_explanation_id, explanation.policy_explanation_id);
    assert.equal(auditAfter.decision, explanation.policy.decision);

    const eventId = `evt_${event.policy_event_id}`;
    const deliveries = database.prepare(`
      SELECT payload_json FROM webhook_deliveries
      WHERE tenant_id=? AND event_id=? AND event_type='policy.dry_run_executed'
    `).all(state.tenantId, eventId);
    for (const delivery of deliveries) {
      const envelope = JSON.parse(String(delivery.payload_json));
      assert.equal(envelope.event_id, eventId);
      assert.equal(envelope.event_type, "policy.dry_run_executed");
      assert.equal(envelope.tenant_id, state.tenantId);
      assert.equal(envelope.object_id, explanation.policy_explanation_id);
      assert.equal(envelope.trace_id, responseBody.trace_id);
      assert.deepEqual(Object.keys(envelope.payload ?? {}).sort(), [...DRY_RUN_EVENT_FIELDS]);
      assert.equal(envelope.payload.request_id, requestId);
      assert.equal(envelope.payload.policy_explanation_id, explanation.policy_explanation_id);
    }
    const attemptCount = deliveries.length
      ? Number(database.prepare(`
          SELECT COUNT(*) AS total FROM webhook_delivery_attempts a
          JOIN webhook_deliveries d
            ON d.tenant_id=a.tenant_id AND d.webhook_delivery_id=a.webhook_delivery_id
          WHERE d.tenant_id=? AND d.event_id=?
        `).get(state.tenantId, eventId).total)
      : 0;
    assert.equal(scanDatabaseSensitiveValues(database, state), 0);
    return {
      explanationRows: 1,
      eventRows: 1,
      auditRows: 1,
      webhookDeliveries: deliveries.length,
      webhookAttempts: attemptCount,
      safeSummaryExact: true,
      databaseSensitiveHits: 0,
    };
  } finally {
    database.close();
  }
}

/**
 * 断言当前页面 DOM、URL 和两种 Web Storage 不含 Prompt sentinel/hash。
 *
 * 返回值只有布尔证据，不读取或输出认证 Token 等其他 storage 内容。
 */
async function assertBrowserPrivacy(page, state) {
  const evidence = await page.evaluate(({ sentinel, digest }) => {
    const values = [document.documentElement.outerHTML, document.URL];
    for (const element of document.querySelectorAll("input, textarea, select")) {
      values.push(element.value ?? "");
    }
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index) ?? "";
        values.push(key, storage.getItem(key) ?? "");
      }
    }
    return {
      domSafe: !values[0].includes(sentinel) && !values[0].includes(digest),
      urlSafe: !values[1].includes(sentinel) && !values[1].includes(digest),
      formAndStorageSafe: values.slice(2).every((value) => (
        !value.includes(sentinel) && !value.includes(digest)
      )),
    };
  }, { sentinel: state.promptSentinel, digest: state.promptHash });
  assert.equal(evidence.domSafe, true);
  assert.equal(evidence.urlSafe, true);
  assert.equal(evidence.formAndStorageSafe, true);
  return evidence;
}

/**
 * 验证三档布局没有根节点溢出、同级块重叠或关键文本裁切。
 *
 * 参数：Page 与期望视口/表单列数。返回：可公开的尺寸和布局计数。
 */
async function assertDryRunLayout(page, viewport, state) {
  const layout = await page.evaluate(() => {
    const root = document.documentElement;
    const fieldset = document.querySelector("#policy-dry-run-form fieldset");
    const columnText = fieldset ? getComputedStyle(fieldset).gridTemplateColumns : "";
    // Chrome 可能返回 repeat/minmax 声明或像素 used-value；顶层空白才代表相邻轨道。
    const repeatedColumns = columnText.match(/^repeat\((\d+),/);
    let topLevelColumns = 0;
    let parenthesisDepth = 0;
    let insideTrack = false;
    for (const character of columnText.trim()) {
      if (character === "(") parenthesisDepth += 1;
      if (character === ")") parenthesisDepth = Math.max(0, parenthesisDepth - 1);
      if (/\s/.test(character) && parenthesisDepth === 0) {
        if (insideTrack) topLevelColumns += 1;
        insideTrack = false;
      } else {
        insideTrack = true;
      }
    }
    if (insideTrack) topLevelColumns += 1;
    const formColumns = repeatedColumns
      ? Number.parseInt(repeatedColumns[1], 10)
      : (columnText === "none" ? 0 : topLevelColumns);
    /** 只把真实占位节点纳入几何检查，避免隐藏表头或零尺寸节点制造溢出/重叠误报。 */
    const visible = (element) => {
      const rectangle = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return style.display !== "none" && style.visibility !== "hidden"
        && rectangle.width > 0 && rectangle.height > 0;
    };
    const offenders = [...document.querySelectorAll(`
      .policy-dry-run-panel, #policy-dry-run-form, .policy-dry-run-result,
      .policy-dry-run-result > section, .policy-dry-run-governance-grid,
      .policy-dry-run-governance-grid > section
    `)].filter(visible).filter((element) => {
      const rectangle = element.getBoundingClientRect();
      return rectangle.left < -1 || rectangle.right > root.clientWidth + 1;
    }).map((element) => element.className || element.id);
    const clipped = [...document.querySelectorAll(`
      #policy-dry-run-form button, #policy-dry-run-form label > span,
      .policy-dry-run-result h3, .policy-dry-run-result strong,
      .policy-dry-run-result [role="cell"], .policy-dry-run-result dd
    `)].filter(visible).filter((element) => (
      element.scrollWidth > element.clientWidth + 1
      || element.scrollHeight > element.clientHeight + 1
    )).map((element) => element.tagName);
    const overlaps = [];
    const parentSelectors = [
      ".policy-dry-run-heading",
      "#policy-dry-run-form fieldset",
      ".policy-dry-run-actions",
      ".policy-dry-run-result",
      ".policy-dry-run-governance-grid",
    ];
    for (const selector of parentSelectors) {
      for (const parent of document.querySelectorAll(selector)) {
        const children = [...parent.children].filter(visible);
        for (let left = 0; left < children.length; left += 1) {
          for (let right = left + 1; right < children.length; right += 1) {
            const a = children[left].getBoundingClientRect();
            const b = children[right].getBoundingClientRect();
            const overlapWidth = Math.min(a.right, b.right) - Math.max(a.left, b.left);
            const overlapHeight = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            if (overlapWidth > 1 && overlapHeight > 1) overlaps.push(selector);
          }
        }
      }
    }
    return {
      innerWidth: window.innerWidth,
      clientWidth: root.clientWidth,
      scrollWidth: root.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      formColumns,
      offenders,
      clipped,
      overlaps,
    };
  });
  // 布局诊断只包含像素、计数和固定 DOM 类名，可安全用于失败定位。
  state.layoutDiagnostic = layout;
  state.checkpoint = `layout_dimensions_${viewport.name}`;
  assert.equal(layout.innerWidth, viewport.width);
  assert.equal(layout.clientWidth, viewport.width);
  state.checkpoint = `layout_overflow_${viewport.name}`;
  assert.ok(layout.scrollWidth <= viewport.width, "document 横向溢出");
  assert.ok(layout.bodyScrollWidth <= viewport.width, "body 横向溢出");
  state.checkpoint = `layout_columns_${viewport.name}`;
  assert.equal(layout.formColumns, viewport.formColumns);
  state.checkpoint = `layout_offenders_${viewport.name}`;
  assert.deepEqual(layout.offenders, []);
  state.checkpoint = `layout_clipped_${viewport.name}`;
  assert.deepEqual(layout.clipped, []);
  state.checkpoint = `layout_overlaps_${viewport.name}`;
  assert.deepEqual(layout.overlaps, []);
  state.layoutDiagnostic = null;
  return layout;
}

/**
 * 保存遮罩身份信息的全页截图，并扫描文件字节不含 sentinel/hash 明文。
 *
 * PNG 压缩扫描是 DOM/表单隐私断言之外的补充证据，不替代页面状态检查。
 */
async function takePrivacySafeScreenshot(page, state, fileName) {
  const userMenu = page.locator(".user-menu");
  assert.equal(await userMenu.count(), 1);
  const screenshotPath = path.join(ARTIFACT_DIR, fileName);
  await page.screenshot({
    path: screenshotPath,
    fullPage: true,
    mask: [userMenu],
    maskColor: "#e5e7eb",
  });
  const bytes = await readFile(screenshotPath);
  assert.equal(bytes.includes(Buffer.from(state.promptSentinel)), false);
  assert.equal(bytes.includes(Buffer.from(state.promptHash)), false);
}

/**
 * 验证响应结构、决策层级、规则顺序和页面可见分区。
 *
 * 返回：不包含任何对象 ID 或完整原因列表的业务摘要。
 */
async function assertRenderedExplanation(page, state, body) {
  const explanation = body.policy_explanation;
  state.checkpoint = "render_base_contract";
  assert.equal(explanation.tenant_id, state.tenantId);
  assert.equal(explanation.status, "completed");
  assert.equal(explanation.simulation_only, true);
  assert.equal(explanation.gateway_parity, "partial");
  assert.equal(explanation.coverage_complete, false);
  assert.equal(explanation.provider_called, false);
  assert.equal(explanation.usage_recorded, false);
  assert.equal(explanation.cost_recorded, false);
  assert.equal(explanation.input_summary.prompt_redacted, true);
  assert.equal(explanation.input_summary.prompt_codepoints, state.promptSentinel.length);
  assert.equal(Object.hasOwn(explanation.input_summary, "prompt"), false);
  state.checkpoint = "render_policy_contract";
  assert.equal(explanation.policy.decision, "allow");
  assert.equal(explanation.policy.matched_policy_id, state.tempPolicy.policy_id);
  assert.ok(Array.isArray(explanation.policy.policy_checks));
  assert.ok(explanation.policy.policy_checks.length >= 1);
  const sortedChecks = [...explanation.policy.policy_checks].sort((left, right) => (
    Number(left.priority) - Number(right.priority)
    || String(left.policy_id).localeCompare(String(right.policy_id))
  ));
  assert.deepEqual(
    explanation.policy.policy_checks.map((item) => item.policy_id),
    sortedChecks.map((item) => item.policy_id),
  );
  state.checkpoint = "render_tree_contract";
  const nodes = explanation.decision_tree?.nodes ?? [];
  assert.ok(nodes.length >= 4);
  const ruleCount = explanation.policy.policy_checks.length;
  const expectedTypes = [
    "policy_set",
    ...Array.from({ length: ruleCount }, () => "policy_rule"),
    "route",
    "dlp",
    "budget",
  ];
  assert.equal(nodes.length, expectedTypes.length);
  assert.deepEqual(nodes.map((node) => node.node_type), expectedTypes);
  for (const [index, node] of nodes.entries()) {
    assert.equal(node.order, index);
    assert.equal(node.node_id, `node_${String(index).padStart(3, "0")}`);
    // 正式 DTO 使用单根扁平树：root 无父节点，其余规则和治理阶段都必须直接归属 root。
    assert.equal(node.parent_id, index === 0 ? null : "node_000");
  }
  state.checkpoint = "render_section_visibility";
  const selectors = [
    "#policy-dry-run-summary-title",
    "#policy-dry-run-compatibility-title",
    "#policy-dry-run-tree-title",
    "#policy-dry-run-rules-title",
    "#policy-dry-run-route-title",
    "#policy-dry-run-dlp-title",
    "#policy-dry-run-budget-title",
  ];
  for (const selector of selectors) await page.locator(selector).waitFor({ state: "visible" });
  state.checkpoint = "render_result_focus";
  assert.equal(await page.locator("#policy-dry-run-summary-title").evaluate(
    (element) => document.activeElement === element,
  ), true);
  state.checkpoint = "render_compatibility_copy";
  assert.match(await page.locator(".policy-dry-run-compatibility").innerText(), /部分覆盖/);
  assert.match(
    await page.locator(".policy-dry-run-compatibility").innerText(),
    explanation.runtime_compatible ? /当前发布合同兼容/ : /当前发布合同不兼容/,
  );
  state.checkpoint = "render_route_copy";
  assert.match(await page.locator("#policy-dry-run-route-title").locator("..").innerText(), /凭据/);
  assert.match(await page.locator("#policy-dry-run-route-title").locator("..").innerText(), /not_evaluated/);
  assert.match(
    await page.locator("#policy-dry-run-route-title").locator("..").innerText(),
    new RegExp(`\\b${explanation.route.status}\\b`),
  );
  state.checkpoint = "render_dlp_budget_copy";
  assert.match(
    await page.locator("#policy-dry-run-dlp-title").locator("..").innerText(),
    new RegExp(`\\b${explanation.dlp.status}\\b`),
  );
  assert.match(
    await page.locator("#policy-dry-run-budget-title").locator("..").innerText(),
    new RegExp(`\\b${explanation.budget.status}\\b`),
  );
  state.checkpoint = "render_tree_rows";
  const treeRows = page.locator(".policy-dry-run-tree li");
  assert.equal(await treeRows.count(), nodes.length);
  assert.deepEqual(
    await treeRows.locator(":scope > span:first-child").allInnerTexts(),
    expectedTypes,
  );
  assert.deepEqual(
    await treeRows.evaluateAll((rows) => rows.map((row) => Number(row.dataset.depth))),
    nodes.map((node) => (node.parent_id ? 1 : 0)),
  );
  state.checkpoint = "render_rule_table";
  assert.ok(await page.locator(".policy-dry-run-rule-table [role='row']").count() >= 2);
  assert.deepEqual(
    await page.locator(".policy-dry-run-rule-table [role='row']:not(.policy-dry-run-table-head) code[role='cell'][data-label='规则 ID']").allInnerTexts(),
    explanation.policy.policy_checks.map((item) => item.policy_rule_id),
  );
  return {
    decision: "allow",
    treeNodeCount: nodes.length,
    checkedPolicyCount: explanation.policy.checked_policy_count,
    runtimeCompatible: explanation.runtime_compatible,
    gatewayParity: "partial",
    routeStatus: explanation.route.status,
    dlpStatus: explanation.dlp.status,
    budgetStatus: explanation.budget.status,
    resultFocused: true,
  };
}

/**
 * 使用真实 admin Console 提交唯一 Prompt，并完成三档布局、隐私、零成本与持久事实验收。
 *
 * 返回：安全的响应/页面/布局/数据库摘要。
 */
async function runAdminAcceptance(browser, state) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const networkViolations = await installLoopbackNetworkGuard(context);
  const page = await context.newPage();
  const signals = captureBrowserSignals(page, state);
  try {
    state.checkpoint = "admin_login";
    const navigation = await loginPage(page, state.accounts.admin, "admin");
    assert.equal(navigation.requestedHash, "#policy-simulator");
    await page.locator(".policy-dry-run-panel #policy-dry-run-form").waitFor({
      state: "visible",
      timeout: 30_000,
    });
    await waitForConsoleStability(page);
    assert.equal(await page.locator(".console-shell").getAttribute("data-active-navigation"), "policy-simulator");

    state.checkpoint = "admin_form_fill";
    await page.locator('select[name="environmentId"]').selectOption(state.context.environmentId);
    await page.locator('select[name="projectId"]').selectOption(state.context.projectId);
    await page.locator('select[name="model"]').selectOption(state.context.model);
    await page.locator('input[name="dataClass"]').fill(state.dataClass);
    await page.locator('input[name="maxOutputTokens"]').fill("32");
    await page.locator('textarea[name="prompt"]').fill(state.promptSentinel);

    const before = snapshotZeroCostFacts();
    const requestPromise = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return url.origin === CONTROL_API_URL
        && url.pathname === "/policies/dry-run"
        && request.method() === "POST";
    }, { timeout: 30_000 });
    const responsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.origin === CONTROL_API_URL
        && url.pathname === "/policies/dry-run"
        && response.request().method() === "POST";
    }, { timeout: 30_000 });
    state.checkpoint = "admin_submit";
    await page.locator('#policy-dry-run-form button[type="submit"]').click();
    const request = await requestPromise;
    const requestPayload = request.postDataJSON();
    assert.equal(requestPayload.prompt, state.promptSentinel);
    assert.equal(requestPayload.environment_id, state.context.environmentId);
    assert.equal(requestPayload.project_id, state.context.projectId);
    assert.equal(requestPayload.model, state.context.model);
    // 请求一发出 textarea 就必须已经被重绘为空，不能等后端 settle 后才清除正文。
    assert.equal(await page.locator('textarea[name="prompt"]').inputValue(), "");

    const response = await responsePromise;
    state.checkpoint = "admin_response_contract";
    assert.equal(response.status(), 200);
    const responseText = await response.text();
    assert.equal(responseText.includes(state.promptSentinel), false);
    assert.equal(responseText.includes(state.promptHash), false);
    const body = JSON.parse(responseText);
    assert.ok(body.request_id && body.trace_id);
    assert.equal(response.headers()["x-request-id"], body.request_id);
    assert.equal(response.headers()["x-trace-id"], body.trace_id);
    assert.equal(response.headers()["cache-control"], "no-store");
    await page.locator("#policy-dry-run-summary-title").waitFor({ state: "visible", timeout: 30_000 });
    state.checkpoint = "admin_rendered_explanation";
    const rendered = await assertRenderedExplanation(page, state, body);
    state.checkpoint = "admin_browser_privacy";
    const browserPrivacy = await assertBrowserPrivacy(page, state);
    assert.equal(signals.sensitiveMessageFound, false);
    assert.deepEqual(signals.errors, []);
    assert.deepEqual(networkViolations, []);

    state.checkpoint = "admin_zero_cost";
    const after = snapshotZeroCostFacts();
    const zeroCost = assertZeroCostUnchanged(before, after);
    state.checkpoint = "admin_persistent_facts";
    const persistentFacts = verifyPersistentFacts(state, body);
    const layouts = {};
    for (const viewport of VIEWPORTS) {
      state.checkpoint = `admin_layout_${viewport.name}`;
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.locator(".policy-dry-run-panel").evaluate(
        (element) => element.scrollIntoView({ block: "start", inline: "nearest" }),
      );
      await page.waitForTimeout(150);
      layouts[viewport.name] = await assertDryRunLayout(page, viewport, state);
      state.checkpoint = `admin_screenshot_${viewport.name}`;
      await takePrivacySafeScreenshot(page, state, `browser-${viewport.name}.png`);
      state.checkpoint = `admin_screenshot_privacy_${viewport.name}`;
      await assertBrowserPrivacy(page, state);
    }
    state.checkpoint = "admin_logout";
    await logoutPage(page);
    return {
      httpStatus: 200,
      requestIdPresent: true,
      traceIdPresent: true,
      cacheControl: "no-store",
      promptClearedBeforeResponse: true,
      responsePromptAbsent: true,
      browserPrivacy,
      browserErrorCount: 0,
      nonLoopbackRequestCount: 0,
      rendered,
      layouts,
      zeroCost,
      persistentFacts,
      screenshotsMasked: VIEWPORTS.length,
    };
  } finally {
    await logoutPageBestEffort(page);
    await context.close();
  }
}

/**
 * 验证 developer/viewer 直达模拟器 hash 时回退 overview，且不生成模拟器 DOM 或请求。
 *
 * 返回：hash、导航、DOM、网络与浏览器错误摘要。
 */
async function runNonAdminAcceptance(browser, state, role) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const networkViolations = await installLoopbackNetworkGuard(context);
  const page = await context.newPage();
  const signals = captureBrowserSignals(page, state);
  const dryRunRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === CONTROL_API_URL && url.pathname === "/policies/dry-run") {
      dryRunRequests.push(request.method());
    }
  });
  try {
    state.checkpoint = `${role}_login_and_fallback`;
    const navigation = await loginPage(page, state.accounts[role], role, POLICY_SIMULATOR_URL);
    await waitForConsoleStability(page);
    assert.equal(navigation.requestedHash, "#policy-simulator");
    assert.equal(await page.evaluate(() => window.location.hash), "");
    assert.equal(await page.locator(".console-shell").getAttribute("data-active-navigation"), "overview");
    assert.equal(await page.locator('.sidebar-link[data-console-nav="policy-simulator"]').count(), 0);
    assert.equal(await page.locator(".policy-dry-run-panel").count(), 0);
    assert.equal(await page.locator("#policy-dry-run-form").count(), 0);
    assert.deepEqual(dryRunRequests, []);
    assert.equal(signals.sensitiveMessageFound, false);
    assert.deepEqual(signals.errors, []);
    assert.deepEqual(networkViolations, []);
    state.checkpoint = `${role}_logout`;
    await logoutPage(page);
    return {
      requestedHash: "#policy-simulator",
      fallbackNavigation: "overview",
      simulatorNavigation: false,
      simulatorDom: false,
      dryRunRequestCount: 0,
      browserErrorCount: 0,
      nonLoopbackRequestCount: 0,
    };
  } finally {
    await logoutPageBestEffort(page);
    await context.close();
  }
}

/**
 * 逐角色直接攻击后端 Dry-Run 入口，证明后端 admin-only 权限不依赖前端隐藏。
 *
 * 返回：两个固定 403 状态；请求/响应正文均不进入输出。
 */
async function verifyBackendRbac(state) {
  const statuses = {};
  for (const role of ["developer", "viewer"]) {
    state.checkpoint = `backend_rbac_${role}`;
    const { body, response } = await controlJson(
      "/policies/dry-run",
      {
        method: "POST",
        headers: {
          ...authHeaders(state.roleTokens[role], `req_prd046_browser_rbac_${role}_${state.suffix}`),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          prompt: state.promptSentinel,
          model: state.context.model,
          project_id: state.context.projectId,
          environment_id: state.context.environmentId,
          data_class: state.dataClass,
          max_output_tokens: 32,
        }),
      },
      403,
      "auth.permission_denied",
    );
    assert.equal(JSON.stringify(body).includes(state.promptSentinel), false);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    statuses[role] = 403;
  }
  // 权限汇总只能暴露角色状态和计数，不能把攻击 Token、Prompt 或响应正文写入证据。
  return { statuses, permissionDeniedCount: 2, promptReflected: false };
}

/**
 * 读取当前租户全部 Policy 的安全清理元数据。
 *
 * 返回值只在内存中用于精确 ID/名称匹配，不进入最终结构化输出。
 */
async function listPoliciesForCleanup(state) {
  const { body } = await controlJson(
    "/policies?status=all",
    { headers: authHeaders(state.bootstrapAdminToken) },
    200,
  );
  return body.policies ?? [];
}

/**
 * 在同一数据库事务内撤销临时三角色 Session 并禁用用户。
 *
 * 这是用户公开 API 尚无禁用入口时的最小兜底；查询严格限定 tenant + 唯一 email/ID，且保留至少一个管理员。
 */
function disableTemporaryUsersAndSessions(state) {
  if (!state.tenantId || !state.userIntents.length) return { usersDisabled: 0, sessionsRevoked: 0 };
  const database = new DatabaseSync(DATABASE_PATH);
  try {
    database.exec("BEGIN IMMEDIATE");
    try {
      const records = [];
      for (const intent of state.userIntents) {
        const rows = database.prepare(`
          SELECT user_id, email, display_name, role FROM users
          WHERE tenant_id=? AND email=?
        `).all(state.tenantId, intent.email);
        assert.ok(rows.length <= 1);
        if (!rows.length) continue;
        assert.equal(rows[0].display_name, intent.displayName);
        assert.equal(rows[0].role, intent.role);
        records.push(String(rows[0].user_id));
      }
      assert.ok(state.userIds.every((userId) => records.includes(userId)));
      if (!records.length) {
        database.exec("COMMIT");
        // 尚未提交任何临时用户时按空白名单收口，禁止扩大到租户级模糊清理。
        return { usersDisabled: 0, sessionsRevoked: 0 };
      }
      const placeholders = records.map(() => "?").join(",");
      const remainingAdmins = database.prepare(`
        SELECT COUNT(*) AS total FROM users
        WHERE tenant_id=? AND role='admin' AND status='active'
          AND user_id NOT IN (${placeholders})
      `).get(state.tenantId, ...records);
      assert.ok(Number(remainingAdmins.total) >= 1);
      const now = new Date().toISOString();
      const sessions = database.prepare(`
        UPDATE sessions SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
        WHERE tenant_id=? AND user_id IN (${placeholders}) AND status='active'
      `).run(now, now, state.tenantId, ...records);
      const users = database.prepare(`
        UPDATE users SET status='disabled', updated_at=?
        WHERE tenant_id=? AND user_id IN (${placeholders}) AND status='active'
      `).run(now, state.tenantId, ...records);
      const active = database.prepare(`
        SELECT COUNT(*) AS total FROM sessions
        WHERE tenant_id=? AND user_id IN (${placeholders}) AND status='active'
      `).get(state.tenantId, ...records);
      assert.equal(Number(active.total), 0);
      database.exec("COMMIT");
      // 清理摘要只返回计数，不暴露用户、Session 或认证材料。
      return { usersDisabled: Number(users.changes), sessionsRevoked: Number(sessions.changes) };
    } catch (error) {
      database.exec("ROLLBACK");
      // 事务失败必须在回滚后继续抛出，让外层固定阶段名阻止验收被误标通过。
      throw error;
    }
  } finally {
    database.close();
  }
}

/**
 * 精确对账并撤销本轮 bootstrap 登录创建的 Session。
 *
 * API logout 仍是首选；此兜底只按 tenant + 已返回 Session ID 查一行，并核对唯一
 * User-Agent 与管理员 user ID，解决“提交成功但响应丢失”造成的错误遗留判断。
 */
function revokeBootstrapSession(state) {
  if (!state.tenantId || !state.bootstrapSessionId) return 0;
  const database = new DatabaseSync(DATABASE_PATH);
  try {
    const rows = database.prepare(`
      SELECT session_id, user_id, user_agent, status FROM sessions
      WHERE tenant_id=? AND session_id=?
    `).all(state.tenantId, state.bootstrapSessionId);
    assert.equal(rows.length, 1);
    assert.equal(String(rows[0].user_id), state.bootstrapAdminUserId);
    assert.equal(String(rows[0].user_agent), state.bootstrapSessionMarker);
    if (rows[0].status !== "active") return 0;
    const now = new Date().toISOString();
    const changed = database.prepare(`
      UPDATE sessions
      SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
      WHERE tenant_id=? AND session_id=? AND status='active'
    `).run(now, now, state.tenantId, state.bootstrapSessionId);
    const active = database.prepare(`
      SELECT COUNT(*) AS total FROM sessions
      WHERE tenant_id=? AND session_id=? AND status='active'
    `).get(state.tenantId, state.bootstrapSessionId);
    assert.equal(Number(active.total), 0);
    return Number(changed.changes);
  } finally {
    database.close();
  }
}

/**
 * 执行一个清理阶段并仅登记固定阶段名。
 *
 * 原始异常可能含内部值，因此失败列表不能复制 error message。
 */
async function attemptCleanup(failures, label, action) {
  try {
    await action();
    return true;
  } catch {
    failures.push(label);
    return false;
  }
}

/**
 * 禁用临时 Policy/Project、恢复原 active Policy、注销 Session 并禁用临时用户。
 *
 * 所有公开管理 API 操作在 bootstrap logout 前完成；各阶段相互独立，单点失败不会跳过后续回收。
 */
async function cleanupState(state) {
  const failures = [];
  const result = {
    temporaryPolicyDisabled: false,
    originalPoliciesRestored: 0,
    temporaryProjectDisabled: false,
    apiSessionsLoggedOut: 0,
    temporaryUsersDisabled: 0,
    temporarySessionsRevoked: 0,
    bootstrapSessionsRevoked: 0,
    failures,
  };
  // bootstrap Token 尚未取得时不存在可授权的业务恢复动作，只能返回当前空清理账本。
  if (!state.bootstrapAdminToken) return result;

  await attemptCleanup(failures, "temporary_policy", async () => {
    const policies = await listPoliciesForCleanup(state);
    const matches = policies.filter((policy) => (
      policy.name === state.tempPolicyIntent.name
      && policy.description === state.tempPolicyIntent.description
    ));
    assert.ok(matches.length <= 1);
    if (!matches.length) {
      assert.equal(state.tempPolicy, null);
      result.temporaryPolicyDisabled = true;
      return;
    }
    const policy = matches[0];
    if (state.tempPolicy) assert.equal(policy.policy_id, state.tempPolicy.policy_id);
    if (policy.status === "active") {
      await controlJson(
        `/policies/${encodeURIComponent(policy.policy_id)}`,
        {
          method: "PATCH",
          headers: {
            ...authHeaders(state.bootstrapAdminToken, `req_prd046_cleanup_policy_${state.suffix}`),
            "Content-Type": "application/json",
            "If-Match": `"${policy.version}"`,
          },
          body: JSON.stringify({ status: "disabled" }),
        },
        200,
      );
    }
    result.temporaryPolicyDisabled = true;
  });

  for (const [index, original] of state.originalActivePolicies.entries()) {
    const restored = await attemptCleanup(failures, `policy_restore_${index}`, async () => {
      const policies = await listPoliciesForCleanup(state);
      const policy = policies.find((item) => item.policy_id === original.policyId);
      assert.ok(policy);
      if (policy.status === "active") return;
      assert.equal(policy.status, "disabled");
      await controlJson(
        `/policies/${encodeURIComponent(original.policyId)}`,
        {
          method: "PATCH",
          headers: {
            ...authHeaders(state.bootstrapAdminToken, `req_prd046_restore_${original.policyId}`),
            "Content-Type": "application/json",
            "If-Match": `"${policy.version}"`,
          },
          body: JSON.stringify({ status: "active" }),
        },
        200,
      );
    });
    if (restored) result.originalPoliciesRestored += 1;
  }

  await attemptCleanup(failures, "temporary_project", async () => {
    const { body: projects } = await controlJson(
      `/projects?status=all&search=${encodeURIComponent(state.tempProjectIntent.projectCode)}`,
      { headers: authHeaders(state.bootstrapAdminToken) },
      200,
    );
    const matches = (projects.projects ?? []).filter((project) => (
      project.project_code === state.tempProjectIntent.projectCode
      && project.name === state.tempProjectIntent.name
    ));
    assert.ok(matches.length <= 1);
    if (!matches.length) {
      assert.equal(state.tempProject, null);
      result.temporaryProjectDisabled = true;
      return;
    }
    const project = matches[0];
    if (project.status === "active") {
      await controlJson(
        `/projects/${encodeURIComponent(project.project_id)}`,
        {
          method: "PATCH",
          headers: {
            ...authHeaders(state.bootstrapAdminToken, `req_prd046_cleanup_project_${state.suffix}`),
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ status: "disabled" }),
        },
        200,
      );
    }
    result.temporaryProjectDisabled = true;
  });

  for (const token of Object.values(state.roleTokens)) {
    try {
      await controlJson("/auth/logout", { method: "POST", headers: authHeaders(token) }, 200);
      result.apiSessionsLoggedOut += 1;
    } catch {
      // 精确用户事务会撤销响应未知或已经注销的剩余活动 Session。
    }
  }
  await attemptCleanup(failures, "temporary_users", async () => {
    const cleaned = disableTemporaryUsersAndSessions(state);
    result.temporaryUsersDisabled = cleaned.usersDisabled;
    result.temporarySessionsRevoked = cleaned.sessionsRevoked;
  });
  try {
    await controlJson(
      "/auth/logout",
      { method: "POST", headers: authHeaders(state.bootstrapAdminToken) },
      200,
    );
    result.apiSessionsLoggedOut += 1;
  } catch {
    // 响应未知时不能假定 Session 仍 active；下面使用唯一账本字段核对最终状态。
  }
  await attemptCleanup(failures, "bootstrap_session", async () => {
    result.bootstrapSessionsRevoked = revokeBootstrapSession(state);
  });
  return result;
}

/**
 * 启动 Chrome 并顺序完成 admin、非 admin、后端 RBAC 和最终清理。
 *
 * 输出始终是脱敏结构化 JSON；任一断言或清理失败都会设置非零退出码。
 */
async function main() {
  const state = createStateLedger();
  let browser = null;
  let acceptance = null;
  let failure = null;
  let cleanup = null;
  try {
    state.phase = "local_scope";
    assertLocalScope();
    await mkdir(ARTIFACT_DIR, { recursive: true });
    state.phase = "browser_launch";
    browser = await chromium.launch({
      executablePath: CHROME_PATH,
      headless: process.env.METAHUB_BROWSER_HEADED !== "1",
    });
    await prepareState(state);
    state.phase = "backend_rbac";
    const backendRbac = await verifyBackendRbac(state);
    state.phase = "admin_browser";
    const admin = await runAdminAcceptance(browser, state);
    state.phase = "developer_browser";
    const developer = await runNonAdminAcceptance(browser, state, "developer");
    state.phase = "viewer_browser";
    const viewer = await runNonAdminAcceptance(browser, state, "viewer");
    acceptance = { admin, developer, viewer, backendRbac };
  } catch (error) {
    failure = {
      phase: state.phase,
      checkpoint: state.checkpoint ?? "unknown",
      ...(state.layoutDiagnostic ? { layout: state.layoutDiagnostic } : {}),
      type: error?.name ?? "Error",
    };
  } finally {
    try {
      await browser?.close();
    } catch {
      failure ??= { phase: "browser_close", type: "BrowserCloseError" };
    }
    state.phase = "cleanup";
    try {
      cleanup = await cleanupState(state);
    } catch (error) {
      cleanup = { failures: ["cleanup_unhandled"] };
      failure ??= { phase: "cleanup", type: error?.name ?? "Error" };
    }
  }
  const passed = Boolean(acceptance) && !failure && cleanup?.failures?.length === 0;
  console.log(JSON.stringify({
    status: passed ? "passed" : "failed",
    ...(acceptance ?? {}),
    cleanup,
    ...(failure ? { failure } : {}),
  }, null, 2));
  if (!passed) process.exitCode = 1;
}

await main();
