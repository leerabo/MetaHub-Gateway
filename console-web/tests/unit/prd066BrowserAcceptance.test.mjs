/**
 * PRD-066 PAGE-093 浏览器验收器纯合同测试。
 *
 * 本文件不启动服务或 Edge、不创建 artifact，也不写运行数据；只验证路径、Origin、
 * CDP 方法关联、权限集合、双视口布局和真实验收脚本的安全流程门禁。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertLayoutMetrics,
  consumeTrackedRequestMethod,
  normalizeLoopbackOrigin,
  normalizePermissionCodes,
  resolveArtifactDirectory,
  samePermissionCodes,
} from "../../../scripts/acceptance/prd066-browser-acceptance.mjs";

/** 从测试文件位置推导仓库根，执行命令所在目录不能改变断言。 */
const REPOSITORY_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
);


/** 构造不含正文的 PAGE-093 布局事实，调用方只覆盖目标失败字段。 */
function layoutMetrics(viewport, overrides = {}) {
  return {
    viewport_width: viewport.width,
    viewport_height: viewport.height,
    document_scroll_width: viewport.width,
    body_scroll_width: viewport.width,
    root_visible: true,
    root_inside_horizontal: true,
    root_overflow: 0,
    toolbar_overflow_count: 0,
    select_count: 2,
    select_overflow_count: 0,
    composer_overflow_count: 0,
    message_count: 2,
    message_overflow_count: 0,
    governance_count: 1,
    governance_overflow_count: 0,
    request_id_wrap_safe: true,
    overlap_count: 0,
    ...overrides,
  };
}


/** 按函数标记提取局部源码，让流程先后顺序断言保持可读。 */
function sourceSection(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0, `missing source marker: ${startMarker}`);
  assert.ok(end > start, `missing source marker: ${endMarker}`);
  return source.slice(start, end);
}


/** artifact 只能写专属根下一层安全 run-id。 */
test("PRD-066 artifact path is fixed under its dedicated one-level root", () => {
  const expectedRoot = path.join(REPOSITORY_ROOT, ".artifacts", "prd066-browser");
  const target = path.join(expectedRoot, "run-contract-001");
  assert.equal(ARTIFACT_ROOT, expectedRoot);
  assert.equal(resolveArtifactDirectory(target), target);
  for (const rejected of [
    expectedRoot,
    path.join(REPOSITORY_ROOT, ".artifacts", "outside", "run-contract-001"),
    path.join(target, "nested"),
    path.join(expectedRoot, "..", "prd066-browser-other", "run-contract-001"),
  ]) {
    assert.throws(
      () => resolveArtifactDirectory(rejected),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "config.artifact_directory"
        && error.code === "outside_or_nested",
    );
  }
});


/** Console 与 Control 只接受无凭据、无路径、无查询的 localhost HTTP Origin。 */
test("PRD-066 loopback origins fail closed on every origin boundary", () => {
  assert.equal(
    normalizeLoopbackOrigin("http://localhost:5173", 5173, "contract.console"),
    "http://localhost:5173",
  );
  const rejected = [
    ["http://127.0.0.1:5173", "host"],
    ["https://localhost:5173", "protocol"],
    ["http://localhost:8001", "port"],
    ["http://user:pass@localhost:5173", "credentials"],
    ["http://localhost:5173/chat", "shape"],
    ["http://localhost:5173/?tenant_id=other", "shape"],
    ["http://localhost:5173/#enterprise-chat", "shape"],
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


/** response 事件没有 method，requestId 关联必须读取一次后立即释放。 */
test("PRD-066 tracked request methods are consumed exactly once", () => {
  const methods = new Map([
    ["request-login", "POST"],
    ["request-options", "GET"],
  ]);
  assert.equal(consumeTrackedRequestMethod(methods, "request-login"), "POST");
  assert.equal(methods.has("request-login"), false);
  assert.equal(methods.get("request-options"), "GET");
  assert.equal(consumeTrackedRequestMethod(methods, "request-login"), "");
  assert.equal(methods.size, 1);
});


/** 权限替换只接受非空、无重复的规范 code，集合比较忽略原顺序。 */
test("PRD-066 permission normalization and CAS equality fail closed", () => {
  const normalized = normalizePermissionCodes(
    ["models.read", "chat.use", "gateway.debug"],
    "contract.permissions",
  );
  assert.deepEqual(normalized, ["chat.use", "gateway.debug", "models.read"]);
  assert.equal(Object.isFrozen(normalized), true);
  assert.equal(
    samePermissionCodes(
      normalized,
      normalizePermissionCodes(
        ["gateway.debug", "models.read", "chat.use"],
        "contract.permissions",
      ),
    ),
    true,
  );
  assert.equal(samePermissionCodes(normalized, normalized.slice(1)), false);
  for (const rejected of [
    [],
    "chat.use",
    ["chat.use", "chat.use"],
    ["Chat.use"],
    ["chat/use"],
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


/** 桌面与移动端都必须容纳工具栏、消息、治理摘要和输入区。 */
test("PRD-066 layout metrics cover both viewports and fail-closed boundaries", () => {
  const desktop = layoutMetrics(VIEWPORTS[0]);
  const mobile = layoutMetrics(VIEWPORTS[1]);
  assert.equal(assertLayoutMetrics(desktop, VIEWPORTS[0]), desktop);
  assert.equal(assertLayoutMetrics(mobile, VIEWPORTS[1]), mobile);

  const failures = [
    [layoutMetrics(VIEWPORTS[0], { viewport_width: 1279 }), VIEWPORTS[0], "viewport"],
    [layoutMetrics(VIEWPORTS[1], { document_scroll_width: 392 }), VIEWPORTS[1], "horizontal_overflow"],
    [layoutMetrics(VIEWPORTS[0], { root_inside_horizontal: false }), VIEWPORTS[0], "root"],
    [layoutMetrics(VIEWPORTS[1], { select_count: 1 }), VIEWPORTS[1], "toolbar"],
    [layoutMetrics(VIEWPORTS[0], { composer_overflow_count: 1 }), VIEWPORTS[0], "composer"],
    [layoutMetrics(VIEWPORTS[1], { message_count: 1 }), VIEWPORTS[1], "messages"],
    [layoutMetrics(VIEWPORTS[0], { governance_overflow_count: 1 }), VIEWPORTS[0], "governance"],
    [layoutMetrics(VIEWPORTS[1], { request_id_wrap_safe: false }), VIEWPORTS[1], "request_id_wrap"],
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
 * 源码门禁锁定真实登录、Chat、清空、双视口、撤权恢复、脱敏证据和注销顺序。
 * 该测试只读取脚本，不执行其中的文件、网络或浏览器流程。
 */
test("PRD-066 acceptance source contains the complete safe browser workflow", async () => {
  const source = await readFile(
    new URL("../../../scripts/acceptance/prd066-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /(?:[\\/])\.tmp(?:[\\/]|["'`])|C:\\\\tmp|\.patch["'`]/i);
  assert.match(source, /\.artifacts["'],\s*["']prd066-browser/);
  assert.match(source, /"http:\/\/localhost:8000"[\s\S]*?"http:\/\/localhost:8001"[\s\S]*?"http:\/\/localhost:5173"/);
  assert.doesNotMatch(source, /(?:from|require\()["'](?:playwright|playwright-core)["']/);
  assert.match(source, /child = launchEdge\(config, tracker\)/);
  assert.match(source, /["']--no-sandbox["']/);
  assert.match(source, /--user-data-dir=/);
  assert.match(source, /ALLOWED_ORIGINS\.has\(target\.origin\)/);
  assert.match(source, /const webSocketUrl = await waitForDebugger\(config\.debugPort\)/);
  assert.match(source, /async function waitForDebugger\(debugPort, timeoutMs = 60000\)/);
  assert.match(source, /browserClient = new CdpClient\(webSocketUrl\)/);
  assert.match(source, /await browserClient\.connect\(\)/);
  assert.match(source, /pageSession = await createPageSession\(browserClient, config\.debugPort\)/);
  assert.match(source, /client = pageSession\.client/);
  assert.match(source, /bindCdpRequestGuard\(client, pageSession, tracker\)/);
  assert.match(source, /\["profile", "browser-cache", "crash-dumps", "browser-home", "Importer_0_4"\]/);

  const execute = sourceSection(source, "async function executeAcceptance", "/** CLI 只输出");
  const orderedMarkers = [
    "loginDeveloper(",
    "openEnterpriseChat(",
    '"browser.chat.first.create"',
    '"browser.chat.first.send"',
    "captureViewport(",
    '"browser.chat.first.clear"',
    '"browser.chat.second.create"',
    '"browser.chat.second.send"',
    "exerciseDeveloperPermissionRevocation(",
    "logoutDeveloper(",
    "assertBrowserPrivacy(",
    "assertNetworkContracts(",
    "publishEvidence(",
  ];
  let previous = -1;
  for (const marker of orderedMarkers) {
    const current = execute.indexOf(marker);
    assert.ok(current > previous, `workflow marker out of order: ${marker}`);
    previous = current;
  }
  assert.match(execute, /for \(const viewport of VIEWPORTS\)[\s\S]*?captureViewport/);
  assert.match(execute, /governed_message_count:\s*2/);

  const tracking = sourceSection(source, "function bindTracker", "/** 通过真实登录表单");
  assert.match(tracking, /target\.pathname === "\/chat\/options" && method === "GET"/);
  assert.match(tracking, /target\.pathname === "\/chat\/sessions" && method === "POST"/);
  assert.match(tracking, /isChatMessagePath\(target\.pathname\) && method === "POST"/);
  assert.match(tracking, /isChatSessionPath\(target\.pathname\) && method === "DELETE"/);
  assert.match(tracking, /messageRequest \? tracker\.credentials : tracker\.secrets/);
  assert.match(tracking, /authorization && target\.origin !== config\.controlOrigin/);

  const send = sourceSection(source, "async function sendChatMessage", "/** 活动会话正文");
  assert.match(send, /textarea\.dispatchEvent\(new Event\("input"/);
  assert.match(send, /form\.requestSubmit\(\)/);
  assert.match(send, /chatMessageStatuses\[expectedMessageCount - 1\] === 200/);
  assert.match(send, /governance_field_count === 6/);
  assert.match(send, /registerRuntimePlaintext\(tracker, facts\.response\)/);
  assert.match(send, /assertActivePlaintextBoundary/);

  const screenshot = sourceSection(source, "async function captureViewport", "/** 构造不含身份");
  assert.match(screenshot, /body\.textContent = "消息正文已脱敏"/);
  assert.match(screenshot, /identity\.style\.visibility = "hidden"/);
  assert.match(screenshot, /"req_prd066_" \+ "a"\.repeat\(180\)/);
  assert.match(screenshot, /Page\.captureScreenshot/);
  assert.match(screenshot, /inspectPngPixels/);

  const permissionRead = sourceSection(
    source,
    "async function readDeveloperRoleSnapshot",
    "/** 用服务端 revision CAS",
  );
  assert.match(permissionRead, /controlJson\(config, "\/permissions"/);
  assert.match(permissionRead, /item\?\.code === "developer"/);
  const permissionReplace = sourceSection(
    source,
    "async function replaceDeveloperRoleSnapshot",
    "/** 刷新当前 Developer",
  );
  assert.match(permissionReplace, /\/roles\/\$\{encodeURIComponent\(snapshot\.roleId\)\}\/permissions/);
  assert.match(permissionReplace, /expected_revision:\s*snapshot\.revision/);
  assert.match(permissionReplace, /revision === snapshot\.revision \+ 1/);

  const revocation = sourceSection(
    source,
    "async function exerciseDeveloperPermissionRevocation",
    "/** 通过真实按钮注销",
  );
  assert.match(revocation, /CHAT_PERMISSION[\s\S]*?"revoke"/);
  assert.match(revocation, /location\.hash !== "#enterprise-chat"/);
  assert.match(revocation, /chatOptionsStatuses\.length === optionsBeforeRevocation/);
  assert.match(revocation, /finally\s*\{[\s\S]*?readDeveloperRoleSnapshot[\s\S]*?original\.permissionCodes[\s\S]*?"restore"/);
  assert.match(revocation, /finally\s*\{[\s\S]*?closeAdminControlSession/);
  assert.match(revocation, /"browser\.permission_revocation\.restored"/);
  assert.match(revocation, /openEnterpriseChat[\s\S]*?"browser\.permission_revocation\.reopened"/);

  const privacy = sourceSection(source, "async function assertBrowserPrivacy", "/** 核对主流程");
  for (const counter of [
    "requestPrivacyViolationCount",
    "consolePrivacyViolationCount",
    "processPrivacyViolationCount",
    "externalRequestCount",
    "forbiddenQueryCount",
  ]) assert.match(privacy, new RegExp(`${counter} === 0`));
  const publishing = sourceSection(source, "async function publishEvidence", "/** 执行登录");
  assert.match(publishing, /tracker\.secrets/);
  assert.match(publishing, /tracker\.browserTokens/);
  assert.match(publishing, /Object\.values\(config\.accounts\)/);
  assert.match(publishing, /forbiddenValues\.every/);
  assert.match(publishing, /browser-evidence\.json/);

  assert.deepEqual(
    VIEWPORTS.map((item) => item.name),
    ["desktop-1280x720", "mobile-390x844"],
  );
});
