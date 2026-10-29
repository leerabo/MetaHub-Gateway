/**
 * PRD-047 PAGE-067 路由策略页真实 Chrome 验收。
 *
 * 脚本读取 runtime acceptance 的脱敏资源 ID，在真实 Console 中验证 admin 写、developer
 * 只读、viewer 无 DOM/预取及三档响应式布局。密码和 Token 只驻留进程内存；输出仅包含
 * 状态、计数、版本和布尔结论。
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { realpathSync, statSync } from "node:fs";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { pathToFileURL } from "node:url";
import { inflateSync } from "node:zlib";

const require = createRequire(import.meta.url);

// 服务地址、敏感环境变量和制品路径集中定义；输出结构绝不能引用密码或 Token 常量。
const CONTROL_API_URL = new URL(
  process.env.METAHUB_CONTROL_API_URL ?? "http://127.0.0.1:8001",
).origin;
const CONSOLE_ROOT_URL = new URL(
  process.env.METAHUB_CONSOLE_URL ?? "http://127.0.0.1:5173",
).origin;
const GATEWAY_API_URL = new URL(
  process.env.METAHUB_GATEWAY_URL ?? "http://localhost:8000",
).origin;
const TENANT_SLUG = (process.env.METAHUB_TENANT_SLUG ?? "default").trim();
const ADMIN_EMAIL = (process.env.METAHUB_ADMIN_EMAIL ?? "admin@metahub.local").trim();
const ADMIN_PASSWORD = String(process.env.METAHUB_PRD047_ADMIN_PASSWORD ?? "");
const DATABASE_PATH = path.resolve(String(process.env.METAHUB_DATABASE_PATH ?? ""));
const RUNTIME_STATE_PATH = path.resolve(String(process.env.METAHUB_PRD047_RUNTIME_STATE ?? ""));
const ARTIFACT_DIR = path.resolve(
  process.env.METAHUB_PRD047_ARTIFACT_DIR ?? "artifacts/prd047",
);
const RESULT_PATH = path.join(ARTIFACT_DIR, "browser-result.json");
const ACCEPTANCE_ROOT = path.resolve("artifacts", "prd047");
const CONTROL_TIMEOUT_MS = 20_000;
const CHROME_PATH = process.env.METAHUB_CHROME_PATH
  ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const ROUTING_POLICY_URL = new URL("#routing-policies", `${CONSOLE_ROOT_URL}/`).toString();

// 视口与设计文档一致；每档都验证页面宽度、操作可达性和稳定截图。
const VIEWPORTS = Object.freeze([
  { name: "desktop-1280x720", width: 1280, height: 720 },
  { name: "intermediate-900x800", width: 900, height: 800 },
  { name: "mobile-390x844", width: 390, height: 844 },
]);

// Runtime 状态只能携带服务生成的短标识符；先限制字符集，后续 SQL 仍全部使用绑定参数。
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const RUNTIME_SUFFIX_PATTERN = /^[a-z0-9]{8,32}$/;
const AUTH_TOKEN_STORAGE_KEY = "metahub.auth_token";
const AUTH_CONTEXT_STORAGE_KEY = "metahub.auth_context";
const AUTH_STORAGE_KEYS = new Set([AUTH_TOKEN_STORAGE_KEY, AUTH_CONTEXT_STORAGE_KEY]);
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);
const BROWSER_ALLOWED_ORIGINS = Object.freeze([CONTROL_API_URL, CONSOLE_ROOT_URL]);
// Console 全局状态只读取四个无业务正文端点；其他 Gateway 路径仍不能进入页面验收网络面。
const GATEWAY_HEALTH_PATHS = new Set(["/health", "/health/live", "/health/ready", "/version"]);
const GENERIC_SECRET_PATTERNS = Object.freeze([
  /\bBearer\s+[A-Za-z0-9._~+/-]{8,}/i,
  // key_prefix 固定保留完整 Key 的前 12 位；mhv_ 后第 9 位起才证明页面泄露了前缀之外的内容。
  /\bmhv_[A-Za-z0-9_-]{9,}/,
  /prd047-prompt-/i,
  /Prd047!/,
  /\bsk[-_][A-Za-z0-9_-]{10,}/i,
  /\bwhsec_[A-Za-z0-9_-]{7,}/i,
]);

/** 判断目标是否位于指定根目录内；使用 path.relative 兼容 Windows 盘符和大小写规则。 */
function pathIsInside(root, target, allowRoot = false) {
  const relative = path.relative(root, target);
  return (allowRoot || relative !== "")
    && relative !== ".."
    && !relative.startsWith(`..${path.sep}`)
    && !path.isAbsolute(relative);
}

/** 校验脱敏状态中的稳定 ID；错误只指出字段类别，不回显可能被篡改的原值。 */
function assertResourceId(value, label) {
  assert.ok(RESOURCE_ID_PATTERN.test(String(value ?? "")), `${label} 标识符非法`);
  return String(value);
}

/** 读取唯一数据库事实；缺失或重复都按固定图校验错误停止，不能继续执行清理。 */
function requiredRow(database, sql, parameters, label) {
  const rows = database.prepare(sql).all(...parameters);
  assert.equal(rows.length, 1, `${label} owner 图不唯一`);
  return rows[0];
}

/** 验证验收回环 URL 的协议、主机、路径和无凭据边界，不把 URL 原文放进断言。 */
function assertAcceptanceUrl(value, expectedPath, label) {
  let target;
  try {
    target = new URL(String(value));
  } catch {
    assert.fail(`${label} URL 非法`);
  }
  assert.ok(
    target.protocol === "http:"
      && LOOPBACK_HOSTS.has(target.hostname)
      && target.username === ""
      && target.password === ""
      && target.search === ""
      && target.hash === ""
      && target.pathname === expectedPath,
    `${label} URL 不属于本轮回环验收`,
  );
}

/** 判断文本是否包含固定哨兵或精确进程内 Secret；返回布尔值而不暴露匹配内容。 */
function hasSensitiveValue(value, exactSecrets) {
  const text = String(value ?? "");
  return exactSecrets.some((secret) => secret && text.includes(secret))
    || GENERIC_SECRET_PATTERNS.some((pattern) => pattern.test(text));
}

/**
 * 核对 DOM、URL 与浏览器存储中的敏感边界。
 *
 * 登录期间只允许当前 UI Session Token 出现在固定 sessionStorage token 槽位；DOM、URL、
 * localStorage、认证上下文和其他槽位都不得出现密码、Token、Virtual Key 或 Prompt 哨兵。
 * 注销后两个认证槽位必须同时消失。断言只输出固定类别，不打印 Secret 或页面正文。
 */
function assertSensitiveSurfaceSnapshot(
  snapshot,
  { exactSecrets = [], activeToken = null, label = "browser" } = {},
) {
  const secrets = [...new Set(exactSecrets.map(String).filter((item) => item.length >= 4))];
  const localEntries = new Map(snapshot.localStorageEntries ?? []);
  const sessionEntries = new Map(snapshot.sessionStorageEntries ?? []);
  const publicSurfaces = [snapshot.domText, snapshot.domHtml, snapshot.url, ...localEntries.values()];
  assert.ok(
    publicSurfaces.every((value) => !hasSensitiveValue(value, secrets)),
    `${label} DOM/URL/localStorage 出现敏感哨兵`,
  );
  assert.ok(
    !localEntries.has(AUTH_TOKEN_STORAGE_KEY) && !localEntries.has(AUTH_CONTEXT_STORAGE_KEY),
    `${label} 禁止把认证态写入 localStorage`,
  );
  // 注销态必须同时移除 Token 与上下文槽位，避免旧角色凭据被下一次页面会话继承。
  if (activeToken === null) {
    assert.ok(
      !sessionEntries.has(AUTH_TOKEN_STORAGE_KEY) && !sessionEntries.has(AUTH_CONTEXT_STORAGE_KEY),
      `${label} 注销后仍保留认证槽位`,
    );
  }

  for (const [key, value] of sessionEntries) {
    // 活跃认证 Token 仅豁免固定槽位，其余存储项仍按敏感值规则逐一检查。
    if (key === AUTH_TOKEN_STORAGE_KEY && activeToken !== null) {
      // 认证 Token 只允许存在于当前页面固定槽位；使用布尔断言避免失败时打印实际值。
      assert.ok(value === activeToken, `${label} sessionStorage Token 与当前角色不一致`);
      continue;
    }
    assert.ok(
      !hasSensitiveValue(value, secrets),
      `${label} sessionStorage 非授权槽位出现敏感哨兵`,
    );
  }
  // 登录态必须成对保留 Token 与角色上下文，防止仅有半套认证状态造成越权误判。
  if (activeToken !== null) {
    assert.ok(sessionEntries.has(AUTH_TOKEN_STORAGE_KEY), `${label} 缺少当前认证 Token 槽位`);
    assert.ok(sessionEntries.has(AUTH_CONTEXT_STORAGE_KEY), `${label} 缺少认证上下文槽位`);
    assert.ok(
      [...sessionEntries.keys()].filter((key) => AUTH_STORAGE_KEYS.has(key)).length === 2,
      `${label} 认证槽位不完整`,
    );
  }
  return {
    domSensitiveHitCount: 0,
    localStorageSensitiveHitCount: 0,
    sessionStorageContractValid: true,
  };
}

/** 从真实页面提取仅供内存断言的表面快照；返回结果绝不进入最终 JSON。 */
async function assertPageSensitiveSurface(page, state, role, { loggedOut = false } = {}) {
  const snapshot = await page.evaluate(() => ({
    domText: document.documentElement?.innerText ?? "",
    domHtml: document.documentElement?.outerHTML ?? "",
    url: window.location.href,
    localStorageEntries: Object.entries(window.localStorage),
    sessionStorageEntries: Object.entries(window.sessionStorage),
  }));
  const activeToken = loggedOut ? null : state.uiTokens.get(role);
  // API 准备会话和真实页面登录会创建不同 Session；这里只能信任已经绑定 owner 的 UI Token。
  if (!loggedOut) {
    assert.ok(
      typeof activeToken === "string" && activeToken.length > 0,
      `${role} 缺少已验证的 UI Session Token`,
    );
  }
  const exactSecrets = [
    ADMIN_PASSWORD,
    state.adminToken,
    ...Object.values(state.roleTokens),
    ...state.uiTokens.values(),
    ...Object.values(state.accounts).map((account) => account.password),
  ];
  return assertSensitiveSurfaceSnapshot(snapshot, {
    exactSecrets,
    activeToken,
    label: role,
  });
}

/** PNG Paeth 预测器；只参与截图像素复原，不输出任何图像正文。 */
function paethPredictor(left, up, upperLeft) {
  const estimate = left + up - upperLeft;
  const leftDistance = Math.abs(estimate - left);
  const upDistance = Math.abs(estimate - up);
  const upperLeftDistance = Math.abs(estimate - upperLeft);
  if (leftDistance <= upDistance && leftDistance <= upperLeftDistance) return left;
  if (upDistance <= upperLeftDistance) return up;
  return upperLeft;
}

/**
 * 解析 Chrome 8-bit RGB/RGBA PNG 并证明尺寸与可见像素存在。
 *
 * 这里不保存像素摘要或 OCR 文本，只返回宽高、字节数和非单色布尔结论，避免截图中的
 * 企业数据进入日志。所有 PNG chunk 长度都先做边界校验，损坏制品必须阻止 passed。
 */
function inspectPngPixels(buffer, { expectedWidth = null, minHeight = 1 } = {}) {
  assert.ok(Buffer.isBuffer(buffer), "截图必须是 PNG Buffer");
  assert.ok(
    buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])),
    "截图 PNG 签名非法",
  );
  let offset = 8;
  let header = null;
  const compressed = [];
  while (offset + 12 <= buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.subarray(offset + 4, offset + 8).toString("ascii");
    const dataStart = offset + 8;
    const dataEnd = dataStart + length;
    assert.ok(dataEnd + 4 <= buffer.length, "截图 PNG chunk 越界");
    const data = buffer.subarray(dataStart, dataEnd);
    if (type === "IHDR") {
      assert.equal(length, 13, "截图 PNG IHDR 长度非法");
      header = {
        width: data.readUInt32BE(0),
        height: data.readUInt32BE(4),
        bitDepth: data[8],
        colorType: data[9],
        compression: data[10],
        filter: data[11],
        interlace: data[12],
      };
    } else if (type === "IDAT") {
      compressed.push(data);
    }
    offset = dataEnd + 4;
    if (type === "IEND") break;
  }
  assert.ok(header && compressed.length > 0, "截图 PNG 缺少 IHDR/IDAT");
  assert.ok(
    header.bitDepth === 8
      && [2, 6].includes(header.colorType)
      && header.compression === 0
      && header.filter === 0
      && header.interlace === 0,
    "截图 PNG 编码不受支持",
  );
  assert.ok(
    header.width >= 1
      && header.width <= 4096
      && header.height >= 1
      && header.height <= 20000
      && buffer.length <= 50 * 1024 * 1024,
    "截图 PNG 尺寸或文件大小超过验收上限",
  );
  if (expectedWidth !== null) assert.equal(header.width, expectedWidth, "截图宽度与视口不一致");
  assert.ok(header.height >= minHeight, "截图高度小于验收视口");
  const channels = header.colorType === 6 ? 4 : 3;
  const stride = header.width * channels;
  const raw = inflateSync(Buffer.concat(compressed));
  assert.equal(raw.length, (stride + 1) * header.height, "截图 PNG 解压长度非法");
  const pixels = Buffer.alloc(stride * header.height);
  for (let row = 0; row < header.height; row += 1) {
    const rawStart = row * (stride + 1);
    const outputStart = row * stride;
    const filterType = raw[rawStart];
    assert.ok(filterType >= 0 && filterType <= 4, "截图 PNG filter 非法");
    for (let column = 0; column < stride; column += 1) {
      const encoded = raw[rawStart + 1 + column];
      const left = column >= channels ? pixels[outputStart + column - channels] : 0;
      const up = row > 0 ? pixels[outputStart - stride + column] : 0;
      const upperLeft = row > 0 && column >= channels
        ? pixels[outputStart - stride + column - channels]
        : 0;
      let predictor = 0;
      if (filterType === 1) predictor = left;
      else if (filterType === 2) predictor = up;
      else if (filterType === 3) predictor = Math.floor((left + up) / 2);
      else if (filterType === 4) predictor = paethPredictor(left, up, upperLeft);
      pixels[outputStart + column] = (encoded + predictor) & 0xff;
    }
  }
  let visiblePixelFound = header.colorType === 2;
  let visualVariance = false;
  const firstColor = pixels.subarray(0, 3);
  for (let offsetIndex = 0; offsetIndex < pixels.length; offsetIndex += channels) {
    if (channels === 4 && pixels[offsetIndex + 3] > 0) visiblePixelFound = true;
    if (
      pixels[offsetIndex] !== firstColor[0]
      || pixels[offsetIndex + 1] !== firstColor[1]
      || pixels[offsetIndex + 2] !== firstColor[2]
    ) {
      visualVariance = true;
      break;
    }
  }
  assert.ok(visiblePixelFound && visualVariance, "截图像素为空或为单色占位图");
  return {
    width: header.width,
    height: header.height,
    byteLength: buffer.length,
    nonBlank: true,
  };
}

/** 验证所有可变目标均位于 PRD-047 制品根和 HTTP 回环地址。 */
function assertLocalScope() {
  assert.equal(process.env.METAHUB_CONFIRM_BROWSER_STATE_MUTATION, "1");
  assert.ok(ADMIN_PASSWORD, "缺少 METAHUB_PRD047_ADMIN_PASSWORD");
  for (const value of [CONTROL_API_URL, CONSOLE_ROOT_URL, GATEWAY_API_URL]) {
    const target = new URL(value);
    assert.equal(target.protocol, "http:", "浏览器验收只允许 HTTP 回环服务");
    assert.ok(LOOPBACK_HOSTS.has(target.hostname), "浏览器验收只允许回环服务");
  }
  assert.ok(pathIsInside(ACCEPTANCE_ROOT, DATABASE_PATH), "数据库必须位于 artifacts/prd047");
  assert.ok(
    pathIsInside(ACCEPTANCE_ROOT, RUNTIME_STATE_PATH),
    "Runtime 状态必须位于 artifacts/prd047",
  );
  assert.ok(
    pathIsInside(ACCEPTANCE_ROOT, ARTIFACT_DIR, true),
    "浏览器制品必须位于 artifacts/prd047",
  );
  assert.notEqual(DATABASE_PATH, RUNTIME_STATE_PATH, "数据库与 Runtime 状态不能是同一文件");
  assert.ok(statSync(DATABASE_PATH).isFile(), "验收数据库文件不存在");
  const runtimeState = statSync(RUNTIME_STATE_PATH);
  assert.ok(runtimeState.isFile(), "Runtime 状态文件不存在");
  assert.ok(runtimeState.size > 0 && runtimeState.size <= 1_048_576, "Runtime 状态文件大小非法");
  assert.ok(statSync(ARTIFACT_DIR).isDirectory(), "浏览器制品目录不存在");
  const realAcceptanceRoot = realpathSync.native(ACCEPTANCE_ROOT);
  assert.ok(
    pathIsInside(realAcceptanceRoot, realpathSync.native(DATABASE_PATH)),
    "数据库符号链接不得逃逸 artifacts/prd047",
  );
  assert.ok(
    pathIsInside(realAcceptanceRoot, realpathSync.native(RUNTIME_STATE_PATH)),
    "Runtime 状态符号链接不得逃逸 artifacts/prd047",
  );
  assert.ok(
    pathIsInside(realAcceptanceRoot, realpathSync.native(ARTIFACT_DIR), true),
    "浏览器制品目录符号链接不得逃逸 artifacts/prd047",
  );
}

/**
 * 在 owner 图已经转成可清理账本后验证 Chrome 前置条件。
 *
 * 参数：``chromePath`` 是本轮实际启动的可执行文件路径。
 * 返回值：无；不存在、不是普通文件或不可读取时抛出固定错误，且不回显主机路径。
 * 关键约束：该函数必须在 ``runOwnedStateLifecycle`` 的 execute 阶段调用，保证失败也会 cleanup。
 */
function assertChromeAvailable(chromePath = CHROME_PATH) {
  let chromeStat = null;
  try {
    chromeStat = statSync(chromePath);
  } catch {
    assert.fail("Chrome 可执行文件不存在");
  }
  assert.ok(chromeStat.isFile(), "Chrome 可执行文件不存在");
}

/**
 * 调用 Control JSON API，并校验统一 HTTP、错误码、request_id 与 trace_id 合同。
 *
 * ``onAcceptedBody`` 在 HTTP 状态确认成功后、其他响应合同断言前执行。创建 User/Session 的调用
 * 用它先登记可信请求侧 owner 和响应稳定 ID，避免后续 owner/request_id 断言失败时丢失清理目标。
 */
async function controlJson(
  pathname,
  options,
  expectedStatus,
  expectedCode = null,
  onAcceptedBody = null,
) {
  const response = await fetch(new URL(pathname, `${CONTROL_API_URL}/`), {
    ...options,
    signal: AbortSignal.timeout(CONTROL_TIMEOUT_MS),
  });
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
  if (onAcceptedBody !== null) onAcceptedBody(body);
  if (expectedCode !== null) assert.equal(body.code, expectedCode);
  assert.ok(body.request_id, `${pathname} 缺少 request_id`);
  assert.ok(body.trace_id, `${pathname} 缺少 trace_id`);
  const expectedRequestId = Object.entries(options.headers ?? {})
    .find(([name]) => name.toLowerCase() === "x-request-id")?.[1];
  if (expectedRequestId) {
    assert.equal(body.request_id, expectedRequestId, `${pathname} request_id 未精确回显`);
  }
  return body;
}

/** 生成内存 Bearer Header；tenant 不得出现在客户端 Header 或 JSON 中。 */
function authHeaders(token, requestId = null) {
  const headers = { Authorization: `Bearer ${token}` };
  if (requestId) headers["X-Request-ID"] = requestId;
  return headers;
}

/** 使用公开认证接口登录；成功状态确认后先登记 Session，再验证剩余响应合同。 */
async function apiLogin(email, password, marker, state, expectedOwner) {
  const requestId = `req_${marker.replaceAll("-", "_")}`;
  const body = await controlJson(
    "/auth/login",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": marker,
        "X-Request-ID": requestId,
      },
      body: JSON.stringify({ tenant_slug: TENANT_SLUG, email, password }),
    },
    200,
    null,
    (body) => registerSession(state, body, expectedOwner),
  );
  // /auth/login 的公开合同平铺 session_id；嵌套 session 仅属于 /auth/me 响应。
  assert.ok(body.access_token && body.user?.user_id && body.session_id);
  return body;
}

/** 登记只允许本轮创建的临时用户；重复 ID 或跨租户响应必须立即停止。 */
function registerTemporaryUser(state, user, expected) {
  const userId = assertResourceId(user?.user_id, "临时用户");
  assert.ok(!state.userOwners.has(userId), "临时用户 ID 重复");
  // owner 只信任请求侧预期值；先入 pending 账本，响应字段即使异常也能由 finally 定位并复验。
  state.userOwners.set(userId, { ...expected });
  state.userIds.add(userId);
  assert.ok(
    user.tenant_id === expected.tenantId
      && user.email === expected.email
      && user.role === expected.role,
    "临时用户 owner/命名不匹配",
  );
  return userId;
}

/** 登记登录返回的 Session owner；cleanup 只按该 tenant + session ID 白名单撤销。 */
function registerSession(state, body, expected) {
  const sessionId = assertResourceId(body?.session_id, "验收 Session");
  assert.ok(!state.sessionOwners.has(sessionId), "验收 Session ID 重复");
  // Session ID 在 owner 响应断言前登记；cleanup 会在写锁内按 tenant/user 重新核对数据库事实。
  state.sessionOwners.set(sessionId, { ...expected });
  state.sessionIds.add(sessionId);
  assert.ok(
    body.user?.tenant_id === expected.tenantId
      && body.user?.user_id === expected.userId
      && body.user?.email === expected.email
      && body.user?.role === expected.role,
    "验收 Session owner 不匹配",
  );
  // 只有 owner 合同通过才把稳定 ID 返回给后续 UI 注销账本；pending 清理登记已经在断言前完成。
  return sessionId;
}

/**
 * 把真实页面登录 Token 绑定到已验证的 UI Session owner，仅保留在当前 Node 进程内存。
 *
 * API 准备 Token 与 UI Token 必须来自不同 Session；跨角色复用或同角色重复绑定都可能把旧权限
 * 带入新 Browser Context，因此一律在进入页面断言前拒绝。错误只报告固定类别，不回显 Token。
 */
function registerUiToken(state, role, value) {
  const token = typeof value === "string" ? value : "";
  assert.ok(token.length > 0, `${role} UI Session Token 缺失`);
  assert.ok(!state.uiTokens.has(role), `${role} UI Session Token 重复绑定`);
  const apiTokens = [state.adminToken, ...Object.values(state.roleTokens)].filter(Boolean);
  assert.ok(!apiTokens.includes(token), `${role} UI Session Token 与 API Session 重复`);
  assert.ok(
    ![...state.uiTokens.values()].includes(token),
    `${role} UI Session Token 与其他角色重复`,
  );
  state.uiTokens.set(role, token);
  // 返回值仅供当前登录调用栈继续校验，调用方不得把它写入日志、JSON 或截图元数据。
  return token;
}

/** 创建浏览器专用角色账号；随机密码和返回 Token 不进入最终 JSON。 */
async function prepareAccounts(state) {
  const admin = await apiLogin(
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    `prd047-browser-admin-${state.suffix}`,
    state,
    {
      tenantId: state.mainTenantId,
      userId: state.adminUserId,
      email: ADMIN_EMAIL,
      role: "admin",
    },
  );
  assert.ok(
    admin.user?.tenant_id === state.mainTenantId
      && admin.user?.email === ADMIN_EMAIL
      && admin.user?.role === "admin",
    "Browser admin owner 不匹配",
  );
  const adminUserId = assertResourceId(admin.user.user_id, "Browser admin");
  state.accounts.admin = {
    email: ADMIN_EMAIL,
    password: ADMIN_PASSWORD,
    userId: adminUserId,
  };
  state.adminToken = admin.access_token;
  for (const role of ["developer", "viewer"]) {
    const email = `prd047-browser-${role}-${state.suffix}@metahub.local`;
    const password = `Prd047!${randomBytes(15).toString("base64url")}`;
    const requestId = `req_prd047_browser_user_${role}_${state.suffix}`;
    const expectedUserOwner = { tenantId: state.mainTenantId, email, role };
    let userId = null;
    const created = await controlJson(
      "/users",
      {
        method: "POST",
        headers: {
          ...authHeaders(state.adminToken, requestId),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          email,
          display_name: `PRD047 browser ${role}`,
          password,
          role,
        }),
      },
      201,
      null,
      (body) => {
        userId = registerTemporaryUser(state, body.user, expectedUserOwner);
      },
    );
    assert.ok(created.user && userId !== null, "临时用户响应合同不完整");
    const login = await apiLogin(
      email,
      password,
      `prd047-browser-${role}-${state.suffix}`,
      state,
      { ...expectedUserOwner, userId },
    );
    state.accounts[role] = { email, password, userId };
    state.roleTokens[role] = login.access_token;
  }
}

/**
 * 给浏览器安装精确 origin 网络硬门禁。
 *
 * 参数：``context`` 是 Playwright Context；``allowedOrigins`` 默认只含当前 Control API 与 Console；
 * ``gatewayHealthOrigin`` 只允许 Console 对四个健康/版本端点执行 GET 及其最小 CORS 预检。
 * 返回值：仅含请求计数和固定违规类别，不记录 URL。即使目标也是 localhost，端口或路径不同也拒绝。
 */
async function installAllowedOriginGuard(
  context,
  allowedOrigins = BROWSER_ALLOWED_ORIGINS,
  gatewayHealthOrigin = GATEWAY_API_URL,
) {
  const exactOrigins = new Set(allowedOrigins.map((value) => new URL(value).origin));
  const exactGatewayOrigin = new URL(gatewayHealthOrigin).origin;
  const evidence = { requestCount: 0, blockedRequestCount: 0, violations: [] };
  await context.route("**/*", async (route) => {
    evidence.requestCount += 1;
    const request = route.request();
    const target = new URL(request.url());
    // Origin、Cookie 和预检头属于安全边界；headers() 可能省略安全相关字段，必须读取完整集合。
    const headers = await request.allHeaders();
    const requestedHeaders = String(headers["access-control-request-headers"] ?? "")
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean)
      .sort();
    const gatewayPathAllowed = GATEWAY_HEALTH_PATHS.has(target.pathname)
      && target.search === ""
      && target.hash === "";
    const gatewaySensitiveHeaderPresent = ["authorization", "cookie", "x-internal-token"]
      .some((name) => Object.hasOwn(headers, name));
    const gatewayGetAllowed = request.method() === "GET"
      && headers.origin === CONSOLE_ROOT_URL
      && !gatewaySensitiveHeaderPresent;
    const gatewayPreflightAllowed = request.method() === "OPTIONS"
      && headers.origin === CONSOLE_ROOT_URL
      && String(headers["access-control-request-method"] ?? "").toUpperCase() === "GET"
      && requestedHeaders.length === 1
      && requestedHeaders[0] === "x-request-id";
    const gatewayHealthAllowed = target.origin === exactGatewayOrigin
      && gatewayPathAllowed
      && (gatewayGetAllowed || gatewayPreflightAllowed);
    if (
      target.protocol === "http:"
      && LOOPBACK_HOSTS.has(target.hostname)
      && target.username === ""
      && target.password === ""
      && (exactOrigins.has(target.origin) || gatewayHealthAllowed)
    ) {
      await route.continue();
      return;
    }
    evidence.blockedRequestCount += 1;
    if (target.origin === exactGatewayOrigin) {
      if (!GATEWAY_HEALTH_PATHS.has(target.pathname)) evidence.violations.push("gateway_path");
      else if (target.search !== "" || target.hash !== "") evidence.violations.push("gateway_query");
      else evidence.violations.push("gateway_method_or_headers");
    } else {
      evidence.violations.push(target.protocol === "http:" ? "origin" : target.protocol);
    }
    await route.abort("blockedbyclient");
  });
  return evidence;
}

/** 捕获浏览器运行错误；原始 console 文本可能含内部信息，因此只保存固定类别。 */
function captureBrowserSignals(page) {
  const signals = { errors: [], consoleErrorCount: 0, http5xxResponseCount: 0 };
  page.on("pageerror", () => signals.errors.push("pageerror"));
  page.on("console", (message) => {
    // 原始 Console 错误可能携带内部响应，只记录固定类别供验收结果计数。
    if (message.type() === "error") {
      signals.consoleErrorCount += 1;
      signals.errors.push("console_error");
    }
  });
  page.on("response", (response) => {
    if (response.status() >= 500) {
      signals.http5xxResponseCount += 1;
      signals.errors.push(`http_${response.status()}_${new URL(response.url()).pathname}`);
    }
  });
  return signals;
}

/** 使用真实 Console 登录表单进入目标 hash，并确认后端返回的角色已经生效。 */
async function loginPage(page, account, expectedRole, state) {
  await page.goto(ROUTING_POLICY_URL, { waitUntil: "domcontentloaded" });
  const requestedHash = await page.evaluate(() => window.location.hash);
  await page.locator("#login-form").waitFor({ state: "visible" });
  await page.locator('[name="tenantSlug"]').fill(TENANT_SLUG);
  await page.locator('[name="email"]').fill(account.email);
  await page.locator('[name="password"]').fill(account.password);
  const loginResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.origin === CONTROL_API_URL
      && url.pathname === "/auth/login"
      && response.request().method() === "POST";
  }, { timeout: 30_000 });
  const meResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.origin === CONTROL_API_URL
      && url.pathname === "/auth/me"
      && response.request().method() === "GET";
  }, { timeout: 30_000 });
  await page.locator('#login-form button[type="submit"]').click();
  const loginResponse = await loginResponsePromise;
  assert.equal(loginResponse.status(), 200);
  const loginBody = await loginResponse.json();
  const storedAccount = state.accounts[expectedRole];
  // HTTP 200 表示 Session 已落库，必须先登记稳定 ID，再校验 owner 与链路字段。
  const sessionId = registerSession(state, loginBody, {
    tenantId: state.mainTenantId,
    userId: storedAccount.userId,
    email: storedAccount.email,
    role: expectedRole,
  });
  state.uiSessionIds.set(expectedRole, sessionId);
  // Token 只在 owner 已登记后进入内存账本；后续任何断言失败都能按 Session ID 精确清理。
  registerUiToken(state, expectedRole, loginBody.access_token);
  assert.ok(loginBody.request_id && loginBody.trace_id);
  assert.equal(loginBody.request_id, loginResponse.headers()["x-request-id"]);
  assert.equal(loginBody.trace_id, loginResponse.headers()["x-trace-id"]);
  const meResponse = await meResponsePromise;
  assert.equal(meResponse.status(), 200);
  const meBody = await meResponse.json();
  // Console 只有在 /auth/me 再次确认 Token 对应的 tenant/user/session/role 后才会保存认证态。
  assert.ok(
    meBody.user?.tenant_id === state.mainTenantId
      && meBody.user?.user_id === storedAccount.userId
      && meBody.user?.email === storedAccount.email
      && meBody.user?.role === expectedRole
      && meBody.tenant?.tenant_id === state.mainTenantId
      && meBody.session?.session_id === sessionId,
    `${expectedRole} UI Session /auth/me owner 不匹配`,
  );
  assert.ok(meBody.request_id && meBody.trace_id);
  assert.equal(meBody.request_id, meResponse.headers()["x-request-id"]);
  assert.equal(meBody.trace_id, meResponse.headers()["x-trace-id"]);
  await page.locator(".console-shell").waitFor({ state: "visible", timeout: 30_000 });
  assert.match(await page.locator(".user-menu").innerText(), new RegExp(`\\b${expectedRole}\\b`));
  return requestedHash;
}

/** 等待页面的首轮 ModelAlias GET 与局部重绘完成。 */
async function waitForRoutingPanel(page, aliasId) {
  await page.locator(".routing-policies-panel #routing-policies-title").waitFor({
    state: "visible",
    timeout: 30_000,
  });
  const row = page.locator(`[data-routing-policy-row="${aliasId}"]`);
  await row.waitFor({ state: "visible", timeout: 30_000 });
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
  await page.waitForTimeout(300);
  return row;
}

/** 注销当前 UI Session；异常路径仍会由数据库白名单清理收口。 */
async function logoutBestEffort(page, state, role) {
  if (state.loggedOutUiSessions.has(role)) return true;
  try {
    const button = page.locator("#logout-button");
    if (await button.count()) {
      await button.click();
      await page.locator("#login-form").waitFor({ state: "visible", timeout: 10_000 });
    }
    await assertPageSensitiveSurface(page, state, role, { loggedOut: true });
    state.loggedOutUiSessions.add(role);
    return true;
  } catch {
    // 最终清理按精确 Session/User ID 执行，不能让 UI 关闭异常覆盖首个验收结论。
    return false;
  }
}

/** 验证布局不横向溢出，路由行和管理员按钮均位于可滚动页面边界内。 */
async function assertLayout(page, row, viewport, requireForm) {
  await row.evaluate((element) => element.scrollIntoView({ block: "start", inline: "nearest" }));
  await page.waitForTimeout(150);
  const metrics = await page.evaluate(() => ({
    viewportWidth: window.innerWidth,
    htmlWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
  }));
  assert.ok(metrics.htmlWidth <= viewport.width, `${viewport.name} html 横向溢出`);
  assert.ok(metrics.bodyWidth <= viewport.width, `${viewport.name} body 横向溢出`);
  const box = await row.boundingBox();
  assert.ok(box && box.width > 0 && box.x >= 0, `${viewport.name} 路由行不可达`);
  assert.ok(box.x + box.width <= viewport.width + 1, `${viewport.name} 路由行越界`);
  if (requireForm) {
    const save = row.locator('button[type="submit"]');
    const saveBox = await save.boundingBox();
    assert.ok(saveBox && saveBox.width >= 44 && saveBox.height >= 30, `${viewport.name} 保存按钮不可达`);
    assert.ok(saveBox.x + saveBox.width <= viewport.width + 1, `${viewport.name} 保存按钮越界`);
  }
  return { horizontalOverflow: false, rowVisible: true, operationReachable: requireForm };
}

/** 截取当前路由策略面板；页面没有 Key/Token/Prompt 字段，因此无需破坏性遮罩。 */
async function takeScreenshot(page, fileName, viewport) {
  await mkdir(ARTIFACT_DIR, { recursive: true });
  const buffer = await page.screenshot({ path: path.join(ARTIFACT_DIR, fileName), fullPage: true });
  assert.ok(buffer.length >= 1024, "截图文件异常过小");
  return inspectPngPixels(buffer, {
    expectedWidth: viewport.width,
    minHeight: viewport.height,
  });
}

/** 管理员显式切换模式并保存，验证 radio 不即刻写入和 PATCH wire/CAS 合同。 */
async function runAdminAcceptance(browser, state) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const network = await installAllowedOriginGuard(context);
  const page = await context.newPage();
  const signals = captureBrowserSignals(page);
  const patchRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith(`/${state.aliasId}/routing-mode`)) patchRequests.push(request);
  });
  try {
    assert.equal(
      await loginPage(page, state.accounts.admin, "admin", state),
      "#routing-policies",
    );
    const row = await waitForRoutingPanel(page, state.aliasId);
    assert.equal(await row.locator("[data-routing-policy-form]").count(), 1);
    const currentText = await row.locator(".routing-policy-current").innerText();
    assert.match(currentText, /可用性优先/);
    const target = row.locator('input[name="routingMode"][value="cost_first"]');
    await target.check();
    await page.waitForTimeout(200);
    assert.equal(patchRequests.length, 0, "切换 radio 不得立即修改线上策略");

    const responsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname.endsWith(`/${state.aliasId}/routing-mode`)
        && response.request().method() === "PATCH";
    }, { timeout: 30_000 });
    await row.locator('button[type="submit"]').click();
    const response = await responsePromise;
    assert.equal(response.status(), 200);
    const responseBody = await response.json();
    assert.equal(responseBody.routing_policy.routing_mode, "cost_first");
    assert.ok(responseBody.routing_policy.version > state.runtimeVersion);
    assert.equal(patchRequests.length, 1);
    const wire = patchRequests[0].postDataJSON();
    assert.deepEqual(Object.keys(wire).sort(), ["expected_version", "routing_mode"]);
    assert.equal(wire.routing_mode, "cost_first");
    assert.equal(Object.hasOwn(wire, "tenant_id"), false);
    assert.ok(patchRequests[0].headers()["x-request-id"]);
    await page.locator(".routing-policy-row-feedback").waitFor({ state: "visible" });
    assert.match(await row.locator(".routing-policy-current").innerText(), /成本优先/);

    const layouts = {};
    const screenshots = {};
    const privacy = await assertPageSensitiveSurface(page, state, "admin");
    for (const viewport of VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      layouts[viewport.name] = await assertLayout(page, row, viewport, true);
      // 每次截图前复扫 DOM/storage，避免响应式重绘把敏感状态意外带回页面。
      await assertPageSensitiveSurface(page, state, "admin");
      screenshots[viewport.name] = await takeScreenshot(
        page,
        `browser-${viewport.name}.png`,
        viewport,
      );
    }
    assert.deepEqual(signals.errors, []);
    assert.deepEqual(network.violations, []);
    assert.equal(await logoutBestEffort(page, state, "admin"), true, "admin 注销/存储清理失败");
    return {
      patchCount: 1,
      radioDoesNotAutoSave: true,
      requestTenantAbsent: true,
      savedMode: "cost_first",
      savedVersion: responseBody.routing_policy.version,
      layouts,
      screenshots,
      privacy,
      browserErrorCount: 0,
      consoleErrorCount: signals.consoleErrorCount,
      http5xxResponseCount: signals.http5xxResponseCount,
      networkRequestCount: network.requestCount,
      nonLoopbackRequestCount: network.blockedRequestCount,
    };
  } finally {
    await logoutBestEffort(page, state, "admin");
    await context.close();
  }
}

/** 开发者可见同一安全摘要，但 DOM 中不得存在 form、radio、保存或取消动作。 */
async function runDeveloperAcceptance(browser, state) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const network = await installAllowedOriginGuard(context);
  const page = await context.newPage();
  const signals = captureBrowserSignals(page);
  const writes = [];
  page.on("request", (request) => {
    if (request.method() === "PATCH" && request.url().includes("/routing-mode")) {
      writes.push(request.method());
    }
  });
  try {
    assert.equal(
      await loginPage(page, state.accounts.developer, "developer", state),
      "#routing-policies",
    );
    const row = await waitForRoutingPanel(page, state.aliasId);
    assert.match(await row.innerText(), /成本优先/);
    assert.equal(await row.locator("[data-routing-policy-form]").count(), 0);
    assert.equal(await row.locator('input[name="routingMode"]').count(), 0);
    assert.equal(await row.locator("button").count(), 0);
    const layout = await assertLayout(
      page,
      row,
      { name: "developer-1280x720", width: 1280, height: 720 },
      false,
    );
    const privacy = await assertPageSensitiveSurface(page, state, "developer");
    assert.deepEqual(writes, []);
    assert.deepEqual(signals.errors, []);
    assert.deepEqual(network.violations, []);
    assert.equal(
      await logoutBestEffort(page, state, "developer"),
      true,
      "developer 注销/存储清理失败",
    );
    return {
      pageVisible: true,
      readonlyDom: true,
      writeRequestCount: 0,
      layout,
      privacy,
      browserErrorCount: 0,
      consoleErrorCount: signals.consoleErrorCount,
      http5xxResponseCount: signals.http5xxResponseCount,
      networkRequestCount: network.requestCount,
      nonLoopbackRequestCount: network.blockedRequestCount,
    };
  } finally {
    await logoutBestEffort(page, state, "developer");
    await context.close();
  }
}

/** viewer 手写 hash 必须回退首个可见页面，且不能预取 ModelAlias 或生成 PAGE-067 DOM。 */
async function runViewerAcceptance(browser, state) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const network = await installAllowedOriginGuard(context);
  const page = await context.newPage();
  const signals = captureBrowserSignals(page);
  const aliasRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === CONTROL_API_URL && url.pathname === "/model-aliases") {
      aliasRequests.push(request.method());
    }
  });
  try {
    assert.equal(
      await loginPage(page, state.accounts.viewer, "viewer", state),
      "#routing-policies",
    );
    await page.waitForTimeout(700);
    assert.equal(await page.locator('.sidebar-link[data-console-nav="routing-policies"]').count(), 0);
    assert.equal(await page.locator(".routing-policies-panel").count(), 0);
    assert.equal(await page.locator("[data-routing-policy-row]").count(), 0);
    assert.deepEqual(aliasRequests, []);
    assert.equal(await page.locator(".console-shell").getAttribute("data-active-navigation"), "overview");
    const privacy = await assertPageSensitiveSurface(page, state, "viewer");
    assert.deepEqual(signals.errors, []);
    assert.deepEqual(network.violations, []);
    assert.equal(await logoutBestEffort(page, state, "viewer"), true, "viewer 注销/存储清理失败");
    return {
      fallbackNavigation: "overview",
      navigationAbsent: true,
      pageDomAbsent: true,
      modelAliasPrefetchCount: 0,
      privacy,
      browserErrorCount: 0,
      consoleErrorCount: signals.consoleErrorCount,
      http5xxResponseCount: signals.http5xxResponseCount,
      networkRequestCount: network.requestCount,
      nonLoopbackRequestCount: network.blockedRequestCount,
    };
  } finally {
    await logoutBestEffort(page, state, "viewer");
    await context.close();
  }
}

/** 直接攻击后端写入口，证明 developer/viewer 的 403 不依赖前端隐藏按钮。 */
async function verifyBackendRbac(state) {
  const statuses = {};
  for (const role of ["developer", "viewer"]) {
    const requestId = `req_prd047_browser_rbac_${role}_${state.suffix}`;
    const body = await controlJson(
      `/model-aliases/${encodeURIComponent(state.aliasId)}/routing-mode`,
      {
        method: "PATCH",
        headers: {
          ...authHeaders(state.roleTokens[role], requestId),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ routing_mode: "fixed", expected_version: 1 }),
      },
      403,
      "auth.permission_denied",
    );
    statuses[role] = body.code;
  }
  return statuses;
}

/** 按阶段校验资源状态；初始化必须 active，cleanup 重入允许对象已经到达目标终态。 */
function assertPhaseStatus(status, phase, activeStatus, terminalStatus, label) {
  const allowed = phase === "initial" ? [activeStatus] : [activeStatus, terminalStatus];
  assert.ok(allowed.includes(status), `${label} 状态不符合验收阶段`);
}

/**
 * 复验 Runtime 状态描述的完整两租户资源图和验收命名。
 *
 * 校验只使用 tenant-qualified 查询与固定名称，不相信状态文件中的任意 ID。cleanup 在
 * BEGIN IMMEDIATE 后再次调用，保证 owner 校验与状态更新之间没有并发写入窗口。
 */
function assertRuntimeGraph(database, runtime, { tenantSlug, phase = "initial" }) {
  assert.ok(runtime && runtime.status === "passed", "Runtime 状态不是 passed");
  const suffix = String(runtime.suffix ?? "");
  assert.ok(RUNTIME_SUFFIX_PATTERN.test(suffix), "Runtime suffix 非法");
  const ids = runtime.resource_ids;
  assert.ok(ids && typeof ids === "object" && !Array.isArray(ids), "Runtime 资源账本非法");
  const requiredKeys = [
    "admin_session_id", "developer_session_id", "viewer_session_id", "other_admin_session_id",
    "alias_id", "routing_policy_id", "model_a_id", "model_b_id", "provider_a_id",
    "provider_b_id", "virtual_key_id", "budget_rule_id", "webhook_endpoint_id", "developer_user_id",
    "viewer_user_id", "other_tenant_id", "other_admin_user_id", "other_environment_id",
    "other_alias_id", "other_routing_policy_id", "other_model_a_id", "other_model_b_id",
    "other_provider_a_id", "other_provider_b_id",
  ];
  const resourceValues = requiredKeys.map((key) => assertResourceId(ids[key], `Runtime ${key}`));
  assert.equal(new Set(resourceValues).size, resourceValues.length, "Runtime 资源 ID 存在重复");
  const availabilityVersion = runtime.availability_first?.version;
  assert.ok(Number.isInteger(availabilityVersion) && availabilityVersion >= 1, "Runtime Policy 版本非法");

  const mainTenant = requiredRow(
    database,
    "SELECT tenant_id, slug, status FROM tenants WHERE slug=?",
    [tenantSlug],
    "默认租户",
  );
  assert.ok(mainTenant.status === "active", "默认租户必须保持 active");
  const mainTenantId = assertResourceId(mainTenant.tenant_id, "默认租户");
  const otherTenant = requiredRow(
    database,
    "SELECT tenant_id, slug, name, status FROM tenants WHERE tenant_id=?",
    [ids.other_tenant_id],
    "第二租户",
  );
  assert.ok(
    otherTenant.tenant_id === `tenant_prd047_${suffix}`
      && otherTenant.slug === `prd047-other-${suffix}`
      && otherTenant.name === `PRD047 other ${suffix}`,
    "第二租户验收命名不匹配",
  );
  assertPhaseStatus(otherTenant.status, phase, "active", "disabled", "第二租户");
  const otherTenantId = String(otherTenant.tenant_id);

  const aliasSql = (
    "SELECT a.tenant_id, a.alias, a.environment, a.environment_id, a.default_model_id, "
    + "a.fallback_model_id, a.description, a.status AS alias_status, e.tenant_id AS env_tenant_id, "
    + "e.code AS env_code, e.status AS env_status, rp.routing_policy_id, "
    + "rp.routing_mode, rp.status AS policy_status, rp.version "
    + "FROM model_aliases a JOIN environments e ON e.environment_id=a.environment_id "
    + "AND e.tenant_id=a.tenant_id JOIN model_alias_routing_policies rp "
    + "ON rp.tenant_id=a.tenant_id AND rp.model_alias_id=a.model_alias_id "
    + "WHERE a.tenant_id=? AND a.model_alias_id=?"
  );
  const mainAlias = requiredRow(database, aliasSql, [mainTenantId, ids.alias_id], "默认 Alias");
  assert.ok(
    mainAlias.alias === `prd047-default-${suffix}`
      && mainAlias.environment === "dev"
      && mainAlias.env_code === "dev"
      && mainAlias.env_tenant_id === mainTenantId
      && mainAlias.default_model_id === ids.model_a_id
      && mainAlias.fallback_model_id === ids.model_b_id
      && mainAlias.description === "PRD-047 runtime acceptance"
      && mainAlias.routing_policy_id === ids.routing_policy_id
      && mainAlias.policy_status === "enabled",
    "默认 Alias/Policy owner 图或命名不匹配",
  );
  assertPhaseStatus(mainAlias.alias_status, phase, "active", "disabled", "默认 Alias");
  assertPhaseStatus(mainAlias.env_status, phase, "active", "active", "默认 Environment");
  if (phase === "initial") {
    assert.ok(
      mainAlias.routing_mode === "availability_first"
        && Number(mainAlias.version) === availabilityVersion,
      "默认 RoutingPolicy 初始版本不匹配",
    );
  } else {
    assert.ok(
      ["availability_first", "cost_first"].includes(mainAlias.routing_mode)
        && Number(mainAlias.version) >= availabilityVersion,
      "默认 RoutingPolicy cleanup 版本不匹配",
    );
  }
  const otherAlias = requiredRow(
    database,
    aliasSql,
    [otherTenantId, ids.other_alias_id],
    "第二租户 Alias",
  );
  assert.ok(
    otherAlias.alias === `prd047-other-${suffix}`
      && otherAlias.environment === "dev"
      && otherAlias.environment_id === ids.other_environment_id
      && otherAlias.env_code === "dev"
      && otherAlias.env_tenant_id === otherTenantId
      && otherAlias.default_model_id === ids.other_model_a_id
      && otherAlias.fallback_model_id === ids.other_model_b_id
      && otherAlias.description === "PRD-047 runtime acceptance"
      && otherAlias.routing_policy_id === ids.other_routing_policy_id
      && otherAlias.policy_status === "enabled"
      && otherAlias.routing_mode === "fixed"
      && Number(otherAlias.version) >= 1,
    "第二租户 Alias/Policy owner 图或命名不匹配",
  );
  assertPhaseStatus(otherAlias.alias_status, phase, "active", "disabled", "第二租户 Alias");
  assertPhaseStatus(otherAlias.env_status, phase, "active", "disabled", "第二租户 Environment");

  const providerSql = (
    "SELECT tenant_id, provider_code, name, base_url, status FROM providers "
    + "WHERE tenant_id=? AND provider_id=?"
  );
  const modelSql = (
    "SELECT tenant_id, provider_id, model_alias, upstream_model_name, display_name, status "
    + "FROM models WHERE tenant_id=? AND model_id=?"
  );
  const providers = [];
  const models = [];
  for (const descriptor of [
    { prefix: "default", label: "a", tenantId: mainTenantId, providerId: ids.provider_a_id, modelId: ids.model_a_id },
    { prefix: "default", label: "b", tenantId: mainTenantId, providerId: ids.provider_b_id, modelId: ids.model_b_id },
    { prefix: "other", label: "a", tenantId: otherTenantId, providerId: ids.other_provider_a_id, modelId: ids.other_model_a_id },
    { prefix: "other", label: "b", tenantId: otherTenantId, providerId: ids.other_provider_b_id, modelId: ids.other_model_b_id },
  ]) {
    const provider = requiredRow(
      database,
      providerSql,
      [descriptor.tenantId, descriptor.providerId],
      "验收 Provider",
    );
    assert.ok(
      provider.provider_code === `prd047_${descriptor.prefix}_${descriptor.label}_${suffix}`
        && provider.name === `PRD047 ${descriptor.prefix} Provider ${descriptor.label.toUpperCase()}`,
      "验收 Provider 命名不匹配",
    );
    assertAcceptanceUrl(
      provider.base_url,
      `/upstream-${descriptor.label}/v1`,
      "验收 Provider",
    );
    assertPhaseStatus(provider.status, phase, "active", "disabled", "验收 Provider");
    providers.push({ id: descriptor.providerId, tenantId: descriptor.tenantId });

    const model = requiredRow(
      database,
      modelSql,
      [descriptor.tenantId, descriptor.modelId],
      "验收 Model",
    );
    assert.ok(
      model.provider_id === descriptor.providerId
        && model.model_alias === `prd047-${descriptor.prefix}-${descriptor.label}-${suffix}`
        && model.upstream_model_name === `prd047-${descriptor.label}`
        && model.display_name === `PRD047 ${descriptor.prefix} Model ${descriptor.label.toUpperCase()}`,
      "验收 Model owner 图或命名不匹配",
    );
    assertPhaseStatus(model.status, phase, "active", "disabled", "验收 Model");
    models.push({ id: descriptor.modelId, tenantId: descriptor.tenantId });
  }

  const userSql = "SELECT tenant_id, email, role, status FROM users WHERE tenant_id=? AND user_id=?";
  const runtimeUsers = new Map();
  for (const descriptor of [
    { id: ids.developer_user_id, tenantId: mainTenantId, email: `prd047-developer-${suffix}@metahub.local`, role: "developer" },
    { id: ids.viewer_user_id, tenantId: mainTenantId, email: `prd047-viewer-${suffix}@metahub.local`, role: "viewer" },
    { id: ids.other_admin_user_id, tenantId: otherTenantId, email: "admin@prd047-other.local", role: "admin" },
  ]) {
    const user = requiredRow(database, userSql, [descriptor.tenantId, descriptor.id], "Runtime 用户");
    assert.ok(user.email === descriptor.email && user.role === descriptor.role, "Runtime 用户命名不匹配");
    assertPhaseStatus(user.status, phase, "active", "disabled", "Runtime 用户");
    runtimeUsers.set(descriptor.id, descriptor);
  }

  const sessionSql = (
    "SELECT s.tenant_id, s.user_id, s.status, u.email, u.role FROM sessions s "
    + "JOIN users u ON u.tenant_id=s.tenant_id AND u.user_id=s.user_id "
    + "WHERE s.tenant_id=? AND s.session_id=?"
  );
  const runtimeSessions = new Map();
  for (const descriptor of [
    { id: ids.admin_session_id, tenantId: mainTenantId, userId: null, email: ADMIN_EMAIL, role: "admin" },
    { id: ids.developer_session_id, tenantId: mainTenantId, userId: ids.developer_user_id, email: `prd047-developer-${suffix}@metahub.local`, role: "developer" },
    { id: ids.viewer_session_id, tenantId: mainTenantId, userId: ids.viewer_user_id, email: `prd047-viewer-${suffix}@metahub.local`, role: "viewer" },
    { id: ids.other_admin_session_id, tenantId: otherTenantId, userId: ids.other_admin_user_id, email: "admin@prd047-other.local", role: "admin" },
  ]) {
    const session = requiredRow(database, sessionSql, [descriptor.tenantId, descriptor.id], "Runtime Session");
    assert.ok(
      session.email === descriptor.email
        && session.role === descriptor.role
        && (descriptor.userId === null || session.user_id === descriptor.userId),
      "Runtime Session owner 不匹配",
    );
    assertPhaseStatus(session.status, phase, "active", "revoked", "Runtime Session");
    runtimeSessions.set(descriptor.id, { ...descriptor, userId: session.user_id });
  }

  const virtualKey = requiredRow(
    database,
    "SELECT tenant_id, environment_id, name, description, status FROM virtual_keys "
      + "WHERE tenant_id=? AND virtual_key_id=?",
    [mainTenantId, ids.virtual_key_id],
    "验收 Virtual Key",
  );
  assert.ok(
    virtualKey.environment_id === mainAlias.environment_id
      && virtualKey.name === `PRD047 runtime ${suffix}`
      && virtualKey.description === "PRD-047 isolated acceptance",
    "验收 Virtual Key owner 图或命名不匹配",
  );
  assertPhaseStatus(virtualKey.status, phase, "active", "disabled", "验收 Virtual Key");

  // Budget 已由 Runtime 在取证后通过公开 API 禁用；Browser 只精确复验，不重新修改财务规则。
  requiredRow(
    database,
    "SELECT br.budget_rule_id FROM budget_rules br "
      + "JOIN environments e ON e.tenant_id=br.tenant_id AND e.environment_id=br.environment_id "
      + "JOIN virtual_keys vk ON vk.tenant_id=br.tenant_id "
      + "AND vk.environment_id=br.environment_id AND vk.virtual_key_id=br.scope_id "
      + "WHERE br.tenant_id=? AND br.environment_id=? AND br.scope_type='virtual_key' "
      + "AND br.scope_id=? AND br.name=? AND br.budget_rule_id=? AND br.status='disabled'",
    [
      mainTenantId,
      mainAlias.environment_id,
      ids.virtual_key_id,
      `PRD047 runtime budget ${suffix}`,
      ids.budget_rule_id,
    ],
    "验收 Budget",
  );
  const webhook = requiredRow(
    database,
    "SELECT tenant_id, name, endpoint_url, event_types_json, status FROM webhook_endpoints "
      + "WHERE tenant_id=? AND webhook_endpoint_id=?",
    [mainTenantId, ids.webhook_endpoint_id],
    "验收 Webhook",
  );
  let webhookEvents = null;
  try {
    webhookEvents = JSON.parse(webhook.event_types_json);
  } catch {
    assert.fail("验收 Webhook 订阅非法");
  }
  assert.ok(
    webhook.name === `PRD047 runtime ${suffix}`
      && Array.isArray(webhookEvents)
      && webhookEvents.length === 1
      && webhookEvents[0] === "route.policy_selected"
      && webhook.status === "disabled",
    "验收 Webhook owner 图或命名不匹配",
  );
  assertAcceptanceUrl(webhook.endpoint_url, "/hook", "验收 Webhook");

  return {
    suffix,
    availabilityVersion,
    mainTenantId,
    otherTenantId,
    mainEnvironmentId: String(mainAlias.environment_id),
    adminUserId: runtimeSessions.get(ids.admin_session_id).userId,
    runtimeUsers,
    runtimeSessions,
    aliases: [
      { id: ids.alias_id, tenantId: mainTenantId },
      { id: ids.other_alias_id, tenantId: otherTenantId },
    ],
    models,
    providers,
  };
}

/** 把已经通过全图验证的 Runtime 摘要转换为 Browser 内存账本。 */
function stateFromRuntime(runtime, ownership) {
  const sessionOwners = new Map(ownership.runtimeSessions);
  const userOwners = new Map(ownership.runtimeUsers);
  return {
    runtime,
    runtimeSuffix: ownership.suffix,
    // Browser 与 Runtime 共用唯一 suffix；精确账本缺失时，cleanup 终态扫描仍能发现本轮命名残留。
    suffix: ownership.suffix,
    aliasId: String(runtime.resource_ids.alias_id),
    runtimeVersion: ownership.availabilityVersion,
    mainTenantId: ownership.mainTenantId,
    otherTenantId: ownership.otherTenantId,
    mainEnvironmentId: ownership.mainEnvironmentId,
    adminUserId: ownership.adminUserId,
    accounts: {},
    roleTokens: {},
    adminToken: null,
    // 页面登录 Token 与 API 准备 Token 分账；只在内存中保存到统一 scrub，不进入结果 JSON。
    uiTokens: new Map(),
    userIds: new Set(userOwners.keys()),
    userOwners,
    sessionIds: new Set(sessionOwners.keys()),
    sessionOwners,
    uiSessionIds: new Map(),
    loggedOutUiSessions: new Set(),
    runtimeResourceIds: runtime.resource_ids,
  };
}

/** 使用指定 SQLite 文件验证 Runtime 状态，供真实入口和无浏览器单测共同复用。 */
function validateRuntimeState(
  runtime,
  { databasePath = DATABASE_PATH, tenantSlug = TENANT_SLUG } = {},
) {
  const database = new DatabaseSync(databasePath, { readOnly: true });
  try {
    // 对外只返回脱敏后的验收状态，数据库行与敏感字段不得进入最终 JSON。
    return stateFromRuntime(runtime, assertRuntimeGraph(database, runtime, { tenantSlug }));
  } finally {
    database.close();
  }
}

/** 读取 runtime 脱敏状态并建立当前浏览器运行的内存账本。 */
async function createState() {
  const runtime = JSON.parse(await readFile(RUNTIME_STATE_PATH, "utf8"));
  return validateRuntimeState(runtime);
}

/** tenant-qualified 地转换单个资源状态，并精确断言影响行数和最终状态。 */
function transitionOwnedStatus(database, descriptor) {
  const before = requiredRow(
    database,
    `SELECT status FROM ${descriptor.table} WHERE tenant_id=? AND ${descriptor.idColumn}=?`,
    [descriptor.tenantId, descriptor.id],
    descriptor.label,
  );
  if (descriptor.assertOwner) descriptor.assertOwner(database);
  const expectedChanges = before.status === descriptor.targetStatus ? 0 : 1;
  const result = database.prepare(descriptor.updateSql).run(
    ...descriptor.updateParameters,
    descriptor.tenantId,
    descriptor.id,
    descriptor.targetStatus,
  );
  assert.equal(Number(result.changes), expectedChanges, `${descriptor.label} cleanup 影响行数不精确`);
  const after = requiredRow(
    database,
    `SELECT status FROM ${descriptor.table} WHERE tenant_id=? AND ${descriptor.idColumn}=?`,
    [descriptor.tenantId, descriptor.id],
    descriptor.label,
  );
  assert.equal(after.status, descriptor.targetStatus, `${descriptor.label} cleanup 终态错误`);
  return expectedChanges;
}

/** 核对全部账本对象终态，并把 enabled Policy 明确限定为 disabled Alias 下不可调用事实。 */
function verifyCleanupTerminalState(database, state) {
  let activeResidueCount = 0;
  const countActive = (table, idColumn, descriptors, activeStatus = "active") => {
    for (const descriptor of descriptors) {
      const row = requiredRow(
        database,
        `SELECT status FROM ${table} WHERE tenant_id=? AND ${idColumn}=?`,
        [descriptor.tenantId, descriptor.id],
        "cleanup 终态",
      );
      if (row.status === activeStatus) activeResidueCount += 1;
    }
  };
  countActive("sessions", "session_id", [...state.sessionOwners.entries()].map(([id, owner]) => ({ id, tenantId: owner.tenantId })));
  countActive("users", "user_id", [...state.userOwners.entries()].map(([id, owner]) => ({ id, tenantId: owner.tenantId })));
  countActive("model_aliases", "model_alias_id", [
    { id: state.runtimeResourceIds.alias_id, tenantId: state.mainTenantId },
    { id: state.runtimeResourceIds.other_alias_id, tenantId: state.otherTenantId },
  ]);
  countActive("models", "model_id", [
    { id: state.runtimeResourceIds.model_a_id, tenantId: state.mainTenantId },
    { id: state.runtimeResourceIds.model_b_id, tenantId: state.mainTenantId },
    { id: state.runtimeResourceIds.other_model_a_id, tenantId: state.otherTenantId },
    { id: state.runtimeResourceIds.other_model_b_id, tenantId: state.otherTenantId },
  ]);
  countActive("providers", "provider_id", [
    { id: state.runtimeResourceIds.provider_a_id, tenantId: state.mainTenantId },
    { id: state.runtimeResourceIds.provider_b_id, tenantId: state.mainTenantId },
    { id: state.runtimeResourceIds.other_provider_a_id, tenantId: state.otherTenantId },
    { id: state.runtimeResourceIds.other_provider_b_id, tenantId: state.otherTenantId },
  ]);
  countActive("virtual_keys", "virtual_key_id", [
    { id: state.runtimeResourceIds.virtual_key_id, tenantId: state.mainTenantId },
  ]);
  countActive("budget_rules", "budget_rule_id", [
    { id: state.runtimeResourceIds.budget_rule_id, tenantId: state.mainTenantId },
  ]);
  countActive("webhook_endpoints", "webhook_endpoint_id", [
    { id: state.runtimeResourceIds.webhook_endpoint_id, tenantId: state.mainTenantId },
  ]);
  countActive("environments", "environment_id", [
    { id: state.runtimeResourceIds.other_environment_id, tenantId: state.otherTenantId },
  ]);
  const otherTenant = requiredRow(
    database,
    "SELECT status FROM tenants WHERE tenant_id=? AND slug=?",
    [state.otherTenantId, `prd047-other-${state.runtimeSuffix}`],
    "第二租户终态",
  );
  // 第二租户本身也属于资源图；未停用时必须计入残留而不能只检查其子资源。
  if (otherTenant.status === "active") activeResidueCount += 1;

  // 精确账本之外再按本轮 suffix 扫描命名空间，防止 Runtime 漏记额外创建的 active 对象。
  const namedResidue = database.prepare(`
    SELECT SUM(count) AS count FROM (
      SELECT COUNT(*) AS count FROM providers
      WHERE status='active' AND provider_code GLOB ?
      UNION ALL SELECT COUNT(*) FROM models
      WHERE status='active' AND model_alias GLOB ?
      UNION ALL SELECT COUNT(*) FROM model_aliases
      WHERE status='active' AND alias GLOB ?
      UNION ALL SELECT COUNT(*) FROM users
      WHERE status='active' AND email GLOB ?
      UNION ALL SELECT COUNT(*) FROM virtual_keys
      WHERE status='active' AND name=?
      UNION ALL SELECT COUNT(*) FROM budget_rules
      WHERE status='active' AND name=?
      UNION ALL SELECT COUNT(*) FROM webhook_endpoints
      WHERE status='active' AND name=?
      UNION ALL SELECT COUNT(*) FROM environments
      WHERE status='active' AND tenant_id=?
      UNION ALL SELECT COUNT(*) FROM tenants
      WHERE status='active' AND slug=?
    )
  `).get(
    `prd047_*_${state.runtimeSuffix}`,
    `prd047-*-${state.runtimeSuffix}`,
    `prd047-*-${state.runtimeSuffix}`,
    `prd047-*-${state.runtimeSuffix}@metahub.local`,
    `PRD047 runtime ${state.runtimeSuffix}`,
    `PRD047 runtime budget ${state.runtimeSuffix}`,
    `PRD047 runtime ${state.runtimeSuffix}`,
    state.otherTenantId,
    `prd047-other-${state.runtimeSuffix}`,
  );
  const namedActiveResidueCount = Number(namedResidue?.count ?? 0);

  const policies = database.prepare(
    "SELECT rp.status AS policy_status, a.status AS alias_status FROM model_alias_routing_policies rp "
      + "JOIN model_aliases a ON a.tenant_id=rp.tenant_id AND a.model_alias_id=rp.model_alias_id "
      + "WHERE (rp.tenant_id=? AND rp.routing_policy_id=?) "
      + "OR (rp.tenant_id=? AND rp.routing_policy_id=?)",
  ).all(
    state.mainTenantId,
    state.runtimeResourceIds.routing_policy_id,
    state.otherTenantId,
    state.runtimeResourceIds.other_routing_policy_id,
  );
  assert.equal(policies.length, 2, "cleanup 后 RoutingPolicy 事实数量错误");
  assert.ok(
    policies.every((row) => row.policy_status === "enabled" && row.alias_status === "disabled"),
    "cleanup 后 RoutingPolicy 仍绑定可调用 Alias",
  );
  const callablePolicyCount = policies.filter(
    (row) => row.policy_status === "enabled" && row.alias_status === "active",
  ).length;
  assert.equal(activeResidueCount, 0, "cleanup 后仍有 active 临时入口资源");
  assert.equal(namedActiveResidueCount, 0, "cleanup 后验收命名空间仍有 active 残留");
  assert.equal(callablePolicyCount, 0, "cleanup 后仍有可调用 RoutingPolicy");
  return {
    activeResidueCount,
    namedActiveResidueCount,
    retainedPolicyCount: policies.length,
    callablePolicyCount,
    terminalStateVerified: true,
  };
}

/** 按 runtime/browser 稳定 ID 禁用临时业务图、账号、第二租户并撤销所有验收 Session。 */
function cleanupState(
  state,
  { databasePath = DATABASE_PATH, tenantSlug = TENANT_SLUG } = {},
) {
  const database = new DatabaseSync(databasePath);
  const now = new Date().toISOString();
  database.exec("PRAGMA foreign_keys = ON");
  database.exec("PRAGMA busy_timeout = 10000");
  database.exec("BEGIN IMMEDIATE");
  const counts = {
    revokedSessionCount: 0,
    disabledTemporaryUserCount: 0,
    disabledAliasCount: 0,
    disabledModelCount: 0,
    disabledProviderCount: 0,
    disabledVirtualKeyCount: 0,
    disabledWebhookCount: 0,
    disabledEnvironmentCount: 0,
    disabledSecondTenantCount: 0,
  };
  try {
    const ownership = assertRuntimeGraph(database, state.runtime, { tenantSlug, phase: "cleanup" });
    assert.ok(
      ownership.mainTenantId === state.mainTenantId
        && ownership.otherTenantId === state.otherTenantId,
      "cleanup 前租户 owner 漂移",
    );

    for (const [sessionId, owner] of state.sessionOwners) {
      counts.revokedSessionCount += transitionOwnedStatus(database, {
        table: "sessions",
        idColumn: "session_id",
        id: sessionId,
        tenantId: owner.tenantId,
        targetStatus: "revoked",
        label: "验收 Session",
        assertOwner: (connection) => {
          const row = requiredRow(
            connection,
            "SELECT user_id FROM sessions WHERE tenant_id=? AND session_id=?",
            [owner.tenantId, sessionId],
            "验收 Session",
          );
          assert.ok(row.user_id === owner.userId, "验收 Session 用户 owner 漂移");
        },
        updateSql: (
          "UPDATE sessions SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=? "
          + "WHERE tenant_id=? AND session_id=? AND status!=?"
        ),
        updateParameters: [now, now],
      });
    }
    for (const [userId, owner] of state.userOwners) {
      counts.disabledTemporaryUserCount += transitionOwnedStatus(database, {
        table: "users",
        idColumn: "user_id",
        id: userId,
        tenantId: owner.tenantId,
        targetStatus: "disabled",
        label: "临时用户",
        assertOwner: (connection) => {
          const row = requiredRow(
            connection,
            "SELECT email, role FROM users WHERE tenant_id=? AND user_id=?",
            [owner.tenantId, userId],
            "临时用户",
          );
          assert.ok(row.email === owner.email && row.role === owner.role, "临时用户命名漂移");
        },
        updateSql: "UPDATE users SET status='disabled', updated_at=? WHERE tenant_id=? AND user_id=? AND status!=?",
        updateParameters: [now],
      });
    }

    for (const descriptor of ownership.aliases) {
      counts.disabledAliasCount += transitionOwnedStatus(database, {
        table: "model_aliases", idColumn: "model_alias_id", ...descriptor,
        targetStatus: "disabled", label: "验收 Alias",
        updateSql: (
          "UPDATE model_aliases SET status='disabled', disabled_at=COALESCE(disabled_at, ?), "
          + "updated_at=? WHERE tenant_id=? AND model_alias_id=? AND status!=?"
        ),
        updateParameters: [now, now],
      });
    }
    for (const descriptor of ownership.models) {
      counts.disabledModelCount += transitionOwnedStatus(database, {
        table: "models", idColumn: "model_id", ...descriptor,
        targetStatus: "disabled", label: "验收 Model",
        updateSql: (
          "UPDATE models SET status='disabled', disabled_at=COALESCE(disabled_at, ?), "
          + "updated_at=? WHERE tenant_id=? AND model_id=? AND status!=?"
        ),
        updateParameters: [now, now],
      });
    }
    for (const descriptor of ownership.providers) {
      counts.disabledProviderCount += transitionOwnedStatus(database, {
        table: "providers", idColumn: "provider_id", ...descriptor,
        targetStatus: "disabled", label: "验收 Provider",
        updateSql: (
          "UPDATE providers SET status='disabled', disabled_at=COALESCE(disabled_at, ?), "
          + "updated_at=? WHERE tenant_id=? AND provider_id=? AND status!=?"
        ),
        updateParameters: [now, now],
      });
    }
    counts.disabledVirtualKeyCount += transitionOwnedStatus(database, {
      table: "virtual_keys", idColumn: "virtual_key_id", id: state.runtimeResourceIds.virtual_key_id,
      tenantId: state.mainTenantId, targetStatus: "disabled", label: "验收 Virtual Key",
      updateSql: (
        "UPDATE virtual_keys SET status='disabled', disabled_at=COALESCE(disabled_at, ?), "
        + "updated_at=? WHERE tenant_id=? AND virtual_key_id=? AND status!=?"
      ),
      updateParameters: [now, now],
    });
    counts.disabledWebhookCount += transitionOwnedStatus(database, {
      table: "webhook_endpoints", idColumn: "webhook_endpoint_id",
      id: state.runtimeResourceIds.webhook_endpoint_id, tenantId: state.mainTenantId,
      targetStatus: "disabled", label: "验收 Webhook",
      updateSql: (
        "UPDATE webhook_endpoints SET status='disabled', updated_at=? "
        + "WHERE tenant_id=? AND webhook_endpoint_id=? AND status!=?"
      ),
      updateParameters: [now],
    });
    counts.disabledEnvironmentCount += transitionOwnedStatus(database, {
      table: "environments", idColumn: "environment_id",
      id: state.runtimeResourceIds.other_environment_id, tenantId: state.otherTenantId,
      targetStatus: "disabled", label: "第二租户 Environment",
      updateSql: (
        "UPDATE environments SET status='disabled', updated_at=? "
        + "WHERE tenant_id=? AND environment_id=? AND status!=?"
      ),
      updateParameters: [now],
    });

    const tenantBefore = requiredRow(
      database,
      "SELECT status FROM tenants WHERE tenant_id=? AND slug=?",
      [state.otherTenantId, `prd047-other-${state.runtimeSuffix}`],
      "第二租户",
    );
    const expectedTenantChanges = tenantBefore.status === "disabled" ? 0 : 1;
    const tenantResult = database.prepare(
      "UPDATE tenants SET status='disabled', updated_at=? WHERE tenant_id=? AND slug=? AND status!='disabled'",
    ).run(now, state.otherTenantId, `prd047-other-${state.runtimeSuffix}`);
    assert.equal(Number(tenantResult.changes), expectedTenantChanges, "第二租户 cleanup 影响行数不精确");
    counts.disabledSecondTenantCount += Number(tenantResult.changes);

    const terminal = verifyCleanupTerminalState(database, state);
    database.exec("COMMIT");
    return { ...counts, ...terminal };
  } catch (error) {
    database.exec("ROLLBACK");
    // 清理失败必须让整个验收失败，不能输出带 passed 的陈旧结果。
    throw error;
  } finally {
    database.close();
  }
}

/** 清空账号密码和 Bearer Token 引用；稳定 ID 清单保留到进程退出供结果核对。 */
function scrubStateSecrets(state) {
  state.accounts = {};
  state.roleTokens = {};
  state.adminToken = null;
  state.uiTokens?.clear();
}

/**
 * 在已验证 owner 图的生命周期内执行 Browser 工作，并保证任意后置失败都进入 cleanup。
 *
 * 参数：state 工厂先建立可信清理账本；execute 执行 Chrome/Playwright/页面验收；cleanup 在写锁内复验。
 * 返回值：成功结果、脱敏后的 state 与 cleanup 证据。主流程失败会原样重抛，清理失败优先阻断通过制品。
 */
async function runOwnedStateLifecycle({
  createStateAction = createState,
  executeAction,
  cleanupAction = cleanupState,
  scrubAction = scrubStateSecrets,
}) {
  assert.equal(typeof executeAction, "function", "Browser 生命周期缺少 executeAction");
  let state = null;
  let failure = null;
  let cleanupFailure = null;
  let result = null;
  let cleanup = null;
  try {
    state = await createStateAction();
    assert.ok(state !== null && typeof state === "object", "Browser owner 状态无效");
    result = await executeAction(state);
  } catch (error) {
    failure = error;
  } finally {
    if (state !== null) {
      try {
        cleanup = await cleanupAction(state);
      } catch (cleanupError) {
        cleanupFailure = cleanupError;
      }
      scrubAction(state);
    }
  }
  if (failure && cleanupFailure) {
    // 主流程与清理同时失败必须显式升级，避免只报告前者而误以为临时账号已撤销。
    throw new Error(
      `PRD-047 browser acceptance and cleanup failed: ${failure.name}/${cleanupFailure.name}`,
    );
  }
  if (cleanupFailure) {
    // 清理失败时禁止生成 passed 制品，即使页面断言已经全部通过。
    throw cleanupFailure;
  }
  if (failure) {
    // 主流程失败保留原始断言栈；状态对象中的密码和 Token 已在 finally 清空。
    throw failure;
  }
  return { state, result, cleanup };
}

/** 启动真实 Chrome，串行执行三角色以避免 Session/页面状态互相污染。 */
async function main() {
  assertLocalScope();
  await mkdir(ARTIFACT_DIR, { recursive: true });
  // 先删除旧结果和截图，本轮失败时不能让上一轮制品伪装成当前通过证据。
  await rm(RESULT_PATH, { force: true });
  for (const viewport of VIEWPORTS) {
    await rm(path.join(ARTIFACT_DIR, `browser-${viewport.name}.png`), { force: true });
  }
  const { state, result, cleanup } = await runOwnedStateLifecycle({
    createStateAction: createState,
    executeAction: async (ownedState) => {
      // Chrome 与 Playwright 前置均在 state 建立后执行；依赖缺失也必须进入统一 cleanup。
      assertChromeAvailable();
      await prepareAccounts(ownedState);
      const { chromium } = require("playwright");
      let browser = null;
      try {
        browser = await chromium.launch({ executablePath: CHROME_PATH, headless: true });
        const admin = await runAdminAcceptance(browser, ownedState);
        const developer = await runDeveloperAcceptance(browser, ownedState);
        const viewer = await runViewerAcceptance(browser, ownedState);
        const backendRbac = await verifyBackendRbac(ownedState);
        return { status: "passed", admin, developer, viewer, backendRbac };
      } finally {
        await browser?.close().catch(() => undefined);
      }
    },
  });
  assert.ok(state !== null && cleanup !== null, "Browser 验收缺少状态或 cleanup 证据");
  assert.equal(state.sessionOwners.size, 10, "Browser 验收 Session 账本数量错误");
  assert.equal(state.userOwners.size, 5, "Browser 验收临时用户账本数量错误");
  assert.equal(state.loggedOutUiSessions.size, 3, "Browser UI Session 未全部正常注销");
  assert.deepEqual(
    {
      revokedSessionCount: cleanup.revokedSessionCount,
      disabledTemporaryUserCount: cleanup.disabledTemporaryUserCount,
      disabledAliasCount: cleanup.disabledAliasCount,
      disabledModelCount: cleanup.disabledModelCount,
      disabledProviderCount: cleanup.disabledProviderCount,
      disabledVirtualKeyCount: cleanup.disabledVirtualKeyCount,
      disabledWebhookCount: cleanup.disabledWebhookCount,
      disabledEnvironmentCount: cleanup.disabledEnvironmentCount,
      disabledSecondTenantCount: cleanup.disabledSecondTenantCount,
    },
    {
      // 三个 UI Session 已由真实 logout 撤销，其余四个 Runtime 与三个 API Session 由 cleanup 撤销。
      revokedSessionCount: 7,
      disabledTemporaryUserCount: 5,
      disabledAliasCount: 2,
      disabledModelCount: 4,
      disabledProviderCount: 4,
      disabledVirtualKeyCount: 1,
      // Runtime 在 Webhook 证据完成后已先禁用 Endpoint，因此 Browser 影响行数必须为 0。
      disabledWebhookCount: 0,
      disabledEnvironmentCount: 1,
      disabledSecondTenantCount: 1,
    },
    "Browser cleanup 精确影响行数错误",
  );
  assert.ok(
    cleanup.activeResidueCount === 0
      && cleanup.namedActiveResidueCount === 0
      && cleanup.retainedPolicyCount === 2
      && cleanup.callablePolicyCount === 0
      && cleanup.terminalStateVerified === true,
    "Browser cleanup 终态证据不完整",
  );
  const output = { ...result, cleanup };
  const encoded = `${JSON.stringify(output, null, 2)}\n`;
  const temporaryResult = path.join(
    ARTIFACT_DIR,
    `.browser-result.${process.pid}.${randomBytes(4).toString("hex")}.tmp`,
  );
  try {
    await writeFile(temporaryResult, encoded, "utf8");
    await rename(temporaryResult, RESULT_PATH);
  } finally {
    await rm(temporaryResult, { force: true });
  }
  process.stdout.write(encoded);
}

// 被测试探针 import 时只暴露清理函数；命令行直接执行才启动真实浏览器验收。
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}

export {
  assertChromeAvailable,
  assertSensitiveSurfaceSnapshot,
  cleanupState,
  inspectPngPixels,
  installAllowedOriginGuard,
  registerSession,
  registerTemporaryUser,
  registerUiToken,
  runOwnedStateLifecycle,
  validateRuntimeState,
};
