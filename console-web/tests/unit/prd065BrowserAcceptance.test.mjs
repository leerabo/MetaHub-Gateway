/**
 * PRD-065 PAGE-092 浏览器验收器纯合同测试。
 *
 * 本文件不启动服务或浏览器、不创建证据目录，也不写入运行数据；只验证证据路径、
 * loopback Origin、CDP 请求方法关联、权限集合、双视口布局和验收脚本源码门禁。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
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
} from "../../../scripts/acceptance/prd065-browser-acceptance.mjs";

/** 从当前测试文件位置推导仓库根，避免执行命令所在目录改变路径断言。 */
const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");


/** 构造不含页面正文的布局事实，调用方只覆盖需要验证的失败字段。 */
function layoutMetrics(viewport, overrides = {}) {
  return {
    viewport_width: viewport.width,
    viewport_height: viewport.height,
    document_scroll_width: viewport.width,
    body_scroll_width: viewport.width,
    root_visible: true,
    root_inside: true,
    root_overflow: 0,
    card_count: 6,
    card_overflow_count: 0,
    tab_overflow_count: 0,
    overlap_count: 0,
    ...overrides,
  };
}


/** 按函数起止标记提取源码片段，让关键流程的先后顺序门禁保持局部且可读。 */
function sourceSection(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0, `missing source marker: ${startMarker}`);
  assert.ok(end > start, `missing source marker: ${endMarker}`);
  return source.slice(start, end);
}


/** 证据目录必须固定在专属根下，并且只能使用一个安全 run-id 层级。 */
test("PRD-065 artifact path is fixed under the dedicated one-level evidence root", () => {
  const expectedRoot = path.join(REPOSITORY_ROOT, ".artifacts", "prd065-browser");
  const target = path.join(expectedRoot, "run-contract-001");
  assert.equal(ARTIFACT_ROOT, expectedRoot);
  assert.equal(resolveArtifactDirectory(target), target);
  for (const rejected of [
    expectedRoot,
    path.join(REPOSITORY_ROOT, ".artifacts", "outside", "run-contract-001"),
    path.join(target, "nested"),
    path.join(expectedRoot, "..", "prd065-browser-other", "run-contract-001"),
  ]) {
    assert.throws(
      () => resolveArtifactDirectory(rejected),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "config.artifact_directory"
        && error.code === "outside_or_nested",
    );
  }
});


/** Console 与 Control 只接受无凭据、无路径、无查询的固定 localhost HTTP Origin。 */
test("PRD-065 loopback origins fail closed on every origin boundary", () => {
  assert.equal(
    normalizeLoopbackOrigin("http://localhost:5173", 5173, "contract.console"),
    "http://localhost:5173",
  );
  const rejected = [
    ["http://127.0.0.1:5173", "host"],
    ["https://localhost:5173", "protocol"],
    ["http://localhost:8001", "port"],
    ["http://user:pass@localhost:5173", "credentials"],
    ["http://localhost:5173/overview", "shape"],
    ["http://localhost:5173/?tenant_id=other", "shape"],
    ["http://localhost:5173/#overview", "shape"],
  ];
  for (const [value, code] of rejected) {
    assert.throws(
      () => normalizeLoopbackOrigin(value, 5173, "contract.console"),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "contract.console"
        && error.code === code,
    );
  }
});


/** CDP 响应不带 method，requestId 关联必须在读取后立即删除，避免状态误配和无界增长。 */
test("PRD-065 tracked request methods are consumed exactly once", () => {
  const methods = new Map([
    ["request-login", "POST"],
    ["request-navigation", "GET"],
  ]);
  assert.equal(consumeTrackedRequestMethod(methods, "request-login"), "POST");
  assert.equal(methods.has("request-login"), false);
  assert.equal(methods.get("request-navigation"), "GET");
  assert.equal(consumeTrackedRequestMethod(methods, "request-login"), "");
  assert.equal(methods.size, 1);
});


/** 权限替换只接受非空、无重复的安全 code，并用规范化集合决定是否需要恢复写。 */
test("PRD-065 permission code normalization and equality fail closed", () => {
  const normalized = normalizePermissionCodes(
    ["billing.read", "billing.anomaly.read", "dashboard.read"],
    "contract.permissions",
  );
  assert.deepEqual(normalized, ["billing.anomaly.read", "billing.read", "dashboard.read"]);
  assert.equal(Object.isFrozen(normalized), true);
  assert.equal(
    samePermissionCodes(
      normalized,
      normalizePermissionCodes(
        ["dashboard.read", "billing.read", "billing.anomaly.read"],
        "contract.permissions",
      ),
    ),
    true,
  );
  assert.equal(samePermissionCodes(normalized, normalized.slice(0, 2)), false);
  for (const rejected of [
    [],
    "billing.read",
    ["billing.read", "billing.read"],
    ["Billing.read"],
    ["billing/read"],
    [true],
    [{}],
  ]) {
    assert.throws(
      () => normalizePermissionCodes(rejected, "contract.permissions"),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "contract.permissions"
        && error.code === "permission_codes",
    );
  }
});


/** 桌面和移动视口都必须容纳六张卡片；任一横溢、裁切或遮挡均关闭验收。 */
test("PRD-065 layout metrics cover both viewports and all fail-closed boundaries", () => {
  const desktop = layoutMetrics(VIEWPORTS[0]);
  const mobile = layoutMetrics(VIEWPORTS[1]);
  assert.equal(assertLayoutMetrics(desktop, VIEWPORTS[0]), desktop);
  assert.equal(assertLayoutMetrics(mobile, VIEWPORTS[1]), mobile);

  const failures = [
    [layoutMetrics(VIEWPORTS[0], { viewport_width: 1279 }), VIEWPORTS[0], "viewport"],
    [layoutMetrics(VIEWPORTS[1], { document_scroll_width: 392 }), VIEWPORTS[1], "horizontal_overflow"],
    [layoutMetrics(VIEWPORTS[0], { root_inside: false }), VIEWPORTS[0], "root"],
    [layoutMetrics(VIEWPORTS[0], { card_count: 5 }), VIEWPORTS[0], "cards"],
    [layoutMetrics(VIEWPORTS[1], { card_overflow_count: 1 }), VIEWPORTS[1], "card_overflow"],
    [layoutMetrics(VIEWPORTS[1], { tab_overflow_count: 1 }), VIEWPORTS[1], "tab_overflow"],
    [layoutMetrics(VIEWPORTS[0], { overlap_count: 1 }), VIEWPORTS[0], "overlap"],
  ];
  for (const [metrics, viewport, code] of failures) {
    assert.throws(
      () => assertLayoutMetrics(metrics, viewport),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === code,
    );
  }
});


/**
 * 源码门禁锁定监听后导航、事件 200、六角色双视口、实时撤权恢复和脱敏证据。
 * 这些断言只读取验收脚本，不执行其中的服务、浏览器、网络或文件写入流程。
 */
test("PRD-065 acceptance source contains the complete safe browser workflow", async () => {
  const source = await readFile(
    new URL("../../../scripts/acceptance/prd065-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /(?:[\\/])\.tmp(?:[\\/]|["'`])|C:\\\\tmp/i);
  assert.match(source, /\.artifacts["'],\s*["']prd065-browser/);
  assert.match(source, /"http:\/\/localhost:8000"[\s\S]*?"http:\/\/localhost:8001"[\s\S]*?"http:\/\/localhost:5173"/);
  assert.doesNotMatch(source, /(?:from|require\()["'](?:playwright|playwright-core)["']/);
  assert.match(source, /child = launchEdge\(config, tracker\)/);
  assert.match(source, /const webSocketUrl = await waitForDebugger\(config\.debugPort\)/);
  assert.match(source, /client = new CdpClient\(webSocketUrl\)/);
  assert.match(source, /await client\.connect\(\)/);
  assert.match(source, /bindCdpRequestGuard\(client, pageSession, tracker\)/);
  assert.match(source, /root\.scrollIntoView\(\{ block: "start", inline: "nearest" \}\)/);
  assert.match(source, /\["profile", "browser-cache", "crash-dumps", "browser-home", "Importer_0_4"\]/);

  const execute = sourceSection(source, "async function executeAcceptance", "/** CLI 入口");
  const connectIndex = execute.indexOf("await client.connect()");
  const sessionIndex = execute.indexOf("pageSession = await createPageSession(client)");
  assert.ok(connectIndex >= 0 && sessionIndex > connectIndex);
  const bindTrackerIndex = execute.indexOf("bindTracker(client, pageSession, config, tracker)");
  const navigateIndex = execute.indexOf('"Page.navigate"');
  assert.ok(bindTrackerIndex >= 0 && navigateIndex > bindTrackerIndex);
  assert.match(execute, /runAndWaitForPageLoad\([\s\S]*?"Page\.navigate"/);

  const tracking = sourceSection(source, "function bindTracker", "/** 通过真实登录表单");
  assert.match(
    tracking,
    /if \(eventType === "dashboard\.role_viewed"\) tracker\.roleViewedEventStatuses\.push\(status\);/,
  );
  assert.equal(
    [...source.matchAll(/roleViewedEventStatuses\[expectedEventCount - 1\] === 200/g)].length,
    3,
  );
  assert.match(execute, /roleViewedEventStatuses\.every\(\(status\) => status === 200\)/);

  assert.deepEqual(Object.keys(ROLE_EXPECTATIONS), [
    "admin",
    "developer",
    "viewer",
    "finance_admin",
    "security_admin",
    "ops_admin",
  ]);
  assert.deepEqual(VIEWPORTS.map((item) => item.name), ["desktop-1280x720", "mobile-390x844"]);
  assert.match(execute, /loginRole\(client, pageSession\.sessionId, config, tracker, "admin"\)/);
  assert.match(
    execute,
    /for \(const role of \["developer", "viewer", "finance_admin", "security_admin", "ops_admin"\]\)/,
  );
  assert.match(execute, /roles\.length === 6/);
  const roleAssertion = sourceSection(source, "async function assertRoleDashboard", "/** 点击 admin 财务分段");
  assert.match(
    roleAssertion,
    /for \(const viewport of VIEWPORTS\)[\s\S]*?setViewport[\s\S]*?assertLayoutMetrics/,
  );

  const permissionRead = sourceSection(source, "async function readFinanceRoleSnapshot", "/** 以服务端 revision CAS");
  assert.match(permissionRead, /controlJson\(config, "\/permissions"/);
  const permissionReplace = sourceSection(source, "async function replaceFinanceRoleSnapshot", "/** 比较两个已规范化权限集合");
  assert.match(permissionReplace, /\/roles\/\$\{encodeURIComponent\(snapshot\.roleId\)\}\/permissions/);
  assert.match(permissionReplace, /expected_revision:\s*snapshot\.revision/);
  assert.match(permissionReplace, /revision === snapshot\.revision \+ 1/);
  const revocation = sourceSection(source, "async function exerciseFinancePermissionRevocation", "/** 设置固定设备指标");
  assert.match(revocation, /FINANCE_VIEW_REVOCATION_PERMISSION[\s\S]*?"revoke"/);
  assert.match(
    revocation,
    /finally\s*\{[\s\S]*?readFinanceRoleSnapshot[\s\S]*?original\.permissionCodes[\s\S]*?"restore"/,
  );
  assert.match(revocation, /finally\s*\{[\s\S]*?closeAdminControlSession/);
  assert.match(revocation, /"browser\.permission_revocation\.restored"/);

  const privacy = sourceSection(source, "async function assertBrowserPrivacy", "/** 通过真实按钮注销");
  for (const counter of [
    "requestPrivacyViolationCount",
    "consolePrivacyViolationCount",
    "processPrivacyViolationCount",
  ]) assert.match(privacy, new RegExp(`${counter} === 0`));
  const publishing = sourceSection(source, "async function publishEvidence", "/** 执行六角色");
  assert.match(publishing, /config\.adminPassword/);
  assert.match(publishing, /config\.runtimePassword/);
  assert.match(publishing, /Object\.values\(config\.accounts\)/);
  assert.match(publishing, /forbiddenValues\.every/);
  assert.match(publishing, /browser-evidence\.json/);
  assert.ok(execute.indexOf("assertBrowserPrivacy") < execute.indexOf("publishEvidence"));
});
