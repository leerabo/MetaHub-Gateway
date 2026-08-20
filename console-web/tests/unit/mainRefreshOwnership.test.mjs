/**
 * 测试职责：锁定控制台注销与迟到刷新之间的认证 owner 顺序，防止旧请求恢复已清理的登录态。
 * 安全边界：测试只读取源码结构，不启动网络、浏览器或真实 Session，也不接触租户业务数据。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";


/** 从主应用源码中提取两个顶层函数之间的固定片段，缺失边界时立即失败。 */
function sourceSection(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0, `missing start marker: ${startMarker}`);
  assert.ok(end > start, `missing end marker: ${endMarker}`);
  return source.slice(start, end);
}


test("logout invalidates refresh ownership before cleanup and network await", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const handler = sourceSection(
    source,
    'logoutButton?.addEventListener("click", async () => {',
    "for (const navigationButton",
  );
  const invalidateIndex = handler.indexOf("const logoutGeneration = ++refreshSequence;");
  const lifecycleIndex = handler.indexOf("gatewayDebugLifecycle.leave();");
  const sidebarAbortIndex = handler.indexOf("consoleSidebarController?.abort();");
  const clearAuthIndex = handler.indexOf("clearAuthSession();");
  const awaitIndex = handler.indexOf("await logoutAdmin");
  const errorGateIndex = handler.indexOf("logoutGeneration !== refreshSequence || authSession.token", awaitIndex);
  const errorRenderIndex = handler.indexOf("renderLogin(authErrorMessage(error)", errorGateIndex);
  const finalGateIndex = handler.indexOf(
    "logoutGeneration !== refreshSequence || authSession.token",
    errorGateIndex + 1,
  );
  const finalRenderIndex = handler.lastIndexOf("renderLogin();");

  assert.ok(invalidateIndex > 0);
  assert.ok(invalidateIndex < lifecycleIndex);
  assert.ok(invalidateIndex < sidebarAbortIndex && sidebarAbortIndex < clearAuthIndex);
  assert.ok(invalidateIndex < clearAuthIndex);
  assert.ok(invalidateIndex < awaitIndex);
  assert.ok(awaitIndex < errorGateIndex && errorGateIndex < errorRenderIndex);
  assert.ok(errorGateIndex < finalGateIndex && finalGateIndex < finalRenderIndex);
});


test("late current-user response is rejected before identity or storage writeback", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const refresh = sourceSection(source, "async function performRefresh(options = {})", "function renderLogin(");
  const frozenTokenIndex = refresh.indexOf("const refreshToken = authSession.token;");
  const ownerClosureIndex = refresh.indexOf("const refreshOwnerCurrent = () =>", frozenTokenIndex);
  const currentUserAwaitIndex = refresh.indexOf("await getCurrentUser({");
  const ownerGateIndex = refresh.indexOf("if (!refreshOwnerCurrent())", currentUserAwaitIndex);
  const identityWriteIndex = refresh.indexOf("clearBudgetStateOnIdentityChange", currentUserAwaitIndex);
  const storageWriteIndex = refresh.indexOf("saveAuthSession(refreshToken, context)", currentUserAwaitIndex);
  const catchIndex = refresh.indexOf("} catch (error)", storageWriteIndex);
  const catchOwnerGateIndex = refresh.indexOf("if (!refreshOwnerCurrent())", catchIndex);
  const catchClearIndex = refresh.indexOf("clearAuthSession();", catchIndex);

  assert.ok(frozenTokenIndex > 0 && frozenTokenIndex < ownerClosureIndex);
  assert.ok(ownerClosureIndex < currentUserAwaitIndex);
  assert.ok(currentUserAwaitIndex < ownerGateIndex && ownerGateIndex < identityWriteIndex);
  assert.ok(ownerGateIndex < storageWriteIndex);
  assert.ok(catchIndex < catchOwnerGateIndex && catchOwnerGateIndex < catchClearIndex);
});


test("console does not register a periodic full-page refresh", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /window\.setInterval\(\(\) => \{[\s\S]*?void refresh\(\);/);
  assert.doesNotMatch(source, /},\s*15000\);/);
});


test("navigation refresh renders the authorized target before active-page hydration", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const refresh = sourceSection(source, "async function performRefresh(options = {})", "function renderLogin(");
  const consumeIndex = refresh.indexOf("const isFastNavigation = nextRefreshIsFastNavigation;");
  const shellIndex = refresh.indexOf("const consoleShellState = await loadConsoleShellState();");
  const fastGateIndex = refresh.indexOf("if (isFastNavigation)", shellIndex);
  const ownerGateIndex = refresh.indexOf("if (!refreshOwnerCurrent() || !authSession.token)", fastGateIndex);
  const renderIndex = refresh.indexOf("renderDeveloperStart(app", ownerGateIndex);
  const bindIndex = refresh.indexOf("bindAuthenticatedEvents(consoleShellState);", renderIndex);
  const dispatcherIndex = refresh.indexOf("await loadAndRenderActiveNavigation({", bindIndex);
  const dispatcherReturnIndex = refresh.indexOf("return;", dispatcherIndex);

  assert.ok(consumeIndex >= 0 && consumeIndex < shellIndex);
  assert.ok(shellIndex < fastGateIndex && fastGateIndex < ownerGateIndex);
  assert.ok(ownerGateIndex < renderIndex && renderIndex < bindIndex);
  assert.ok(bindIndex < dispatcherIndex && dispatcherIndex < dispatcherReturnIndex);
  assert.doesNotMatch(refresh.slice(fastGateIndex, dispatcherIndex), /void refresh\(\)/);
});


test("active-page dispatcher does not preload unrelated navigation data", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const dispatcher = sourceSection(
    source,
    "async function loadAndRenderActiveNavigation({",
    "function renderLogin(",
  );
  const pageCase = (id, nextId) => dispatcher.slice(
    dispatcher.indexOf(`case "${id}":`),
    dispatcher.indexOf(`case "${nextId}":`),
  );

  const overview = pageCase("overview", "tenant");
  assert.match(overview, /loadRoleDashboardState/);
  assert.doesNotMatch(overview, /loadProvidersState|loadUsageState|loadProjectState/);

  const providers = pageCase("providers", "provider-health");
  assert.match(providers, /loadProvidersState/);
  assert.doesNotMatch(providers, /loadModelsState|loadUsageState|loadProjectState/);

  const usage = pageCase("usage", "finops-ledger");
  assert.match(usage, /loadUsageState/);
  assert.doesNotMatch(usage, /loadProvidersState|loadProjectState|loadVirtualKeysState/);

  const assistant = pageCase("admin-assistant", "system-status");
  assert.match(assistant, /loadAdminAssistantState/);
  assert.doesNotMatch(assistant, /await|getServiceStatuses|getConfigSummary/);
});


test("console preferences are cached only for the complete authorization identity", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const shellLoader = sourceSection(
    source,
    "async function loadConsoleShellState()",
    "async function switchConsoleNavigation",
  );
  assert.match(shellLoader, /roleDashboardIdentitySnapshot\(authSession\.context\)/);
  assert.match(shellLoader, /identity === consolePreferencesIdentity/);
  assert.match(shellLoader, /cachedPreferences[\s\S]*Promise\.resolve\(cachedPreferences\)[\s\S]*getConsolePreferences/);
  assert.match(source, /function renderLogin[\s\S]*consolePreferencesIdentity = "";[\s\S]*consolePreferencesCache = null;/);
});


test("navigation click hydrates only a backend-visible target before deferred telemetry", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const navigation = sourceSection(source, "async function switchConsoleNavigation", "function readExpandedConsoleGroupKey");
  const transitionIndex = navigation.indexOf("renderConsoleNavigationTransition(button");
  const authorizedItemIndex = navigation.indexOf("const projectedNavigationItem =", transitionIndex);
  const visibilityIndex = navigation.indexOf("item.visible !== false && item.disabled !== true", authorizedItemIndex);
  const renderIndex = navigation.indexOf("renderDeveloperStart(app", visibilityIndex);
  const bindIndex = navigation.indexOf("bindAuthenticatedEvents(authorizedProjectedState);", renderIndex);
  const hydrateIndex = navigation.indexOf("await loadAndRenderActiveNavigation({", bindIndex);
  const sideEffectIndex = navigation.indexOf("scheduleConsoleNavigationSideEffects({", hydrateIndex);

  assert.ok(transitionIndex >= 0 && transitionIndex < authorizedItemIndex);
  assert.ok(authorizedItemIndex < visibilityIndex && visibilityIndex < renderIndex);
  assert.ok(renderIndex < bindIndex && bindIndex < hydrateIndex && hydrateIndex < sideEffectIndex);
  assert.doesNotMatch(navigation.slice(bindIndex, hydrateIndex), /getCurrentUser|loadConsoleShellState|refresh\(\)/);
  assert.match(source, /clearTimeout\(consoleNavigationSideEffectTimer\)/);
  assert.match(source, /CONSOLE_NAVIGATION_SIDE_EFFECT_DELAY_MS = 5_000/);
  const sideEffects = sourceSection(
    source,
    "function scheduleConsoleNavigationSideEffects",
    "function readExpandedConsoleGroupKey",
  );
  assert.ok(sideEffects.indexOf("await updateConsolePreferences") < sideEffects.indexOf("await recordConsolePageView"));
  assert.match(sideEffects, /if \(!navigationOwnerCurrent\(\)\) return;/);
});


test("sidebar groups use one persisted accordion expansion", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const toggle = sourceSection(source, "function toggleConsoleSidebarGroup", "/** 导航请求期间");
  assert.match(toggle, /document\.querySelectorAll\("\[data-sidebar-group\]"\)/);
  assert.match(toggle, /const shouldExpand = button\.getAttribute\("aria-expanded"\) !== "true"/);
  assert.match(toggle, /expandedConsoleGroupKey = shouldExpand \? key : null/);
  assert.match(toggle, /persistExpandedConsoleGroupKey\(\)/);
});


test("superseded login success is rejected after each await and before session commit", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const login = sourceSection(source, "function renderLogin(", "/** 生成当前邀请弹窗 owner 输入");
  const submitIndex = login.indexOf('form?.addEventListener("submit", async (event) => {');
  const ownerIndex = login.indexOf("const loginOwner = Object.freeze({ generation: ++loginGeneration });", submitIndex);
  const loginAwaitIndex = login.indexOf("const login = await loginAdmin({", ownerIndex);
  const loginGateIndex = login.indexOf("if (!loginOwnerCurrent())", loginAwaitIndex);
  const currentUserAwaitIndex = login.indexOf("const context = await getCurrentUser", loginGateIndex);
  const currentUserGateIndex = login.indexOf("if (!loginOwnerCurrent())", currentUserAwaitIndex);
  const commitGateIndex = login.indexOf("if (!loginOwnerCurrent())", currentUserGateIndex + 1);
  const storageWriteIndex = login.indexOf("saveAuthSession(loginToken, context);", commitGateIndex);
  const memoryWriteIndex = login.indexOf("authSession = { token: loginToken, context };", storageWriteIndex);
  const ownerInvalidateIndex = login.indexOf("loginGeneration += 1;", memoryWriteIndex);
  const refreshInvalidateIndex = login.indexOf("refreshSequence += 1;", ownerInvalidateIndex);

  assert.ok(submitIndex >= 0 && submitIndex < ownerIndex);
  assert.ok(ownerIndex < loginAwaitIndex && loginAwaitIndex < loginGateIndex);
  assert.ok(loginGateIndex < currentUserAwaitIndex && currentUserAwaitIndex < currentUserGateIndex);
  assert.ok(currentUserGateIndex < commitGateIndex && commitGateIndex < storageWriteIndex);
  assert.ok(storageWriteIndex < memoryWriteIndex && memoryWriteIndex < ownerInvalidateIndex);
  assert.ok(ownerInvalidateIndex < refreshInvalidateIndex);
});


test("superseded login failure cannot clear or redraw a newer session", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const login = sourceSection(source, "function renderLogin(", "/** 生成当前邀请弹窗 owner 输入");
  const ownerClosureIndex = login.indexOf("const loginOwnerCurrent = () => (");
  const generationGateIndex = login.indexOf("loginOwner.generation === loginGeneration", ownerClosureIndex);
  const sessionGateIndex = login.indexOf("&& !authSession.token", generationGateIndex);
  const catchIndex = login.indexOf("} catch (err) {");
  const catchOwnerGateIndex = login.indexOf("if (!loginOwnerCurrent())", catchIndex);
  const catchClearIndex = login.indexOf("clearAuthSession();", catchOwnerGateIndex);
  const catchMemoryClearIndex = login.indexOf("authSession = { token: null, context: null };", catchClearIndex);
  const catchRenderIndex = login.indexOf("renderLogin(authErrorMessage(err)", catchMemoryClearIndex);

  assert.ok(ownerClosureIndex >= 0 && ownerClosureIndex < generationGateIndex);
  assert.ok(generationGateIndex < sessionGateIndex && sessionGateIndex < catchIndex);
  assert.ok(catchIndex < catchOwnerGateIndex && catchOwnerGateIndex < catchClearIndex);
  assert.ok(catchClearIndex < catchMemoryClearIndex && catchMemoryClearIndex < catchRenderIndex);
});


test("login page replacement and each new submit invalidate earlier login owners", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const declarationIndex = source.indexOf("let loginGeneration = 0;");
  const login = sourceSection(source, "function renderLogin(", "/** 生成当前邀请弹窗 owner 输入");
  const renderInvalidateIndex = login.indexOf("loginGeneration += 1;");
  const lifecycleCleanupIndex = login.indexOf("gatewayDebugLifecycle.leave();");
  const pageRenderIndex = login.indexOf("renderLoginPage(app");
  const submitIndex = login.indexOf('form?.addEventListener("submit", async (event) => {');
  const submitInvalidateIndex = login.indexOf("generation: ++loginGeneration", submitIndex);

  assert.ok(declarationIndex >= 0);
  assert.ok(renderInvalidateIndex >= 0 && renderInvalidateIndex < lifecycleCleanupIndex);
  assert.ok(renderInvalidateIndex < pageRenderIndex && pageRenderIndex < submitIndex);
  assert.ok(submitIndex < submitInvalidateIndex);
});
