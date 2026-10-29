/**
 * 测试职责：锁定控制台注销与迟到刷新之间的认证 owner 顺序，防止旧请求恢复已清理的登录态。
 * 安全边界：源码结构和隔离函数测试不启动网络、浏览器或真实 Session，不接触租户业务数据。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";


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
  const cleanupIndex = handler.indexOf("clearConsoleSessionState();");
  const clearAuthIndex = handler.indexOf("clearAuthSession();");
  const awaitIndex = handler.indexOf("await logoutIdentity");
  const finalGateIndex = handler.indexOf("logoutGeneration !== refreshSequence || hasWorkspaceConsoleSession(authSession)", awaitIndex);
  const finalRenderIndex = handler.indexOf('window.location.replace("./")', finalGateIndex);

  assert.ok(invalidateIndex > 0);
  assert.ok(invalidateIndex < cleanupIndex && cleanupIndex < clearAuthIndex);
  assert.ok(invalidateIndex < clearAuthIndex);
  assert.ok(invalidateIndex < awaitIndex);
  assert.ok(awaitIndex < finalGateIndex && finalGateIndex < finalRenderIndex);
  const cleanup = sourceSection(source, "function clearConsoleSessionState(", "function invitationOwnerInput(");
  assert.match(cleanup, /gatewayDebugLifecycle\.leave\(\)/);
  assert.match(cleanup, /consoleSidebarController\?\.abort\(\)/);
});


test("late Cookie shell is rejected before session installation and old state cleanup", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const refresh = sourceSection(source, "async function performRefresh(options = {})", "function renderLogin(");
  const ownerClosureIndex = refresh.indexOf("const refreshOwnerCurrent = () =>");
  const awaitIndex = refresh.indexOf("await loadConsoleShellState(refreshOwnerCurrent)");
  const ownerGateIndex = refresh.indexOf("if (!refreshOwnerCurrent())", awaitIndex);
  const validateIndex = refresh.indexOf("workspaceConsoleSessionFromShell(consoleShellState)");
  const cleanupIndex = refresh.indexOf("clearConsoleSessionState({ clearNavigation: false })");
  const installIndex = refresh.indexOf("authSession = session;");
  assert.ok(ownerClosureIndex >= 0 && ownerClosureIndex < awaitIndex);
  assert.ok(awaitIndex < ownerGateIndex && ownerGateIndex < validateIndex);
  assert.ok(validateIndex < cleanupIndex && cleanupIndex < installIndex);
  assert.doesNotMatch(refresh, /getCurrentUser|readAuthSession|saveAuthSession/);
});


test("console does not register a periodic full-page refresh", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /window\.setInterval\(\(\) => \{[\s\S]*?void refresh\(\);/);
  assert.doesNotMatch(source, /},\s*15000\);/);
});


test("navigation refresh authorizes the target before route-only hydration", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const refresh = sourceSection(source, "async function performRefresh(options = {})", "function renderLogin(");
  const ownerIndex = refresh.indexOf("const refreshOwnerCurrent = () =>");
  const shellIndex = refresh.indexOf("const consoleShellState = await loadConsoleShellState(refreshOwnerCurrent);");
  const ownerGateIndex = refresh.indexOf("if (!refreshOwnerCurrent())", shellIndex);
  const dispatcherIndex = refresh.indexOf("await loadAndRenderActiveNavigation({", ownerGateIndex);
  const dispatcherReturnIndex = refresh.indexOf("return;", dispatcherIndex);
  const dispatcher = sourceSection(
    source,
    "async function loadAndRenderActiveNavigation({",
    "function renderLogin(",
  );
  const routeSwitchIndex = dispatcher.indexOf("switch (activeNavigationId)");
  const finalGateIndex = dispatcher.indexOf(
    "if (!hasWorkspaceConsoleSession(authSession) || refreshId !== refreshSequence || !refreshOwnerCurrent()) return;",
  );
  const renderIndex = dispatcher.indexOf("renderDeveloperStart(app", finalGateIndex);
  const bindIndex = dispatcher.indexOf("bindAuthenticatedEvents(consoleShellState);", renderIndex);

  assert.ok(ownerIndex >= 0 && ownerIndex < shellIndex);
  assert.ok(shellIndex < ownerGateIndex && ownerGateIndex < dispatcherIndex);
  assert.ok(dispatcherIndex < dispatcherReturnIndex);
  assert.ok(routeSwitchIndex >= 0 && routeSwitchIndex < finalGateIndex);
  assert.ok(finalGateIndex < renderIndex && renderIndex < bindIndex);
  assert.doesNotMatch(refresh.slice(shellIndex, dispatcherIndex), /void refresh\(\)/);
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
  assert.match(overview, /loadWorkspaceConsoleExperienceState/);
  assert.doesNotMatch(overview, /loadProvidersState|loadUsageState|loadProjectState/);

  const providers = pageCase("providers", "provider-health");
  assert.match(providers, /loadProvidersState/);
  assert.doesNotMatch(providers, /loadModelsState|loadUsageState|loadProjectState/);

  const models = pageCase("models", "response-cache");
  assert.match(models, /loadProvidersState\(\{ includeKeys: false \}\)/u);

  const usage = pageCase("usage", "workspace-subscription");
  assert.match(usage, /loadUsageState/);
  assert.doesNotMatch(usage, /loadProvidersState|loadProjectState|loadVirtualKeysState/);

  const assistant = pageCase("admin-assistant", "system-status");
  assert.match(assistant, /loadWorkspaceConsoleExperienceState/);
  assert.doesNotMatch(assistant, /getServiceStatuses|getConfigSummary|loadProvidersState/);

  const quickstart = pageCase("quickstart", "sdk-examples");
  assert.match(quickstart, /workspaceNavigationController\.loadGettingStarted/);
  assert.doesNotMatch(quickstart, /loadProvidersState|loadUsageState|loadProjectState/);
});


test("workspace navigation preferences come only from the current canonical response", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const shellLoader = sourceSection(
    source,
    "async function loadConsoleShellState(",
    "async function switchConsoleNavigation",
  );
  assert.match(shellLoader, /workspaceNavigationController\.loadNavigation\(\)/);
  assert.match(shellLoader, /preferences: \{ preference: navigation\.preference \}/);
  assert.match(shellLoader, /const savedExpandedGroup = preference\.expanded_group_code/);
  assert.match(shellLoader, /Number\(preference\.revision \?\? 0\) <= 1/);
  assert.doesNotMatch(shellLoader, /expandedConsoleGroupKey =/);
  assert.doesNotMatch(
    shellLoader,
    /consolePreferencesCache|getConsolePreferences|localStorage|sessionStorage/,
  );
  assert.match(
    source,
    /function renderLogin[\s\S]*workspaceNavigationController\.leave\(\)/,
  );
});

test("shell loader discards stale authentication failures before clearing the current session", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const shellLoader = sourceSection(source, "async function loadConsoleShellState(",
    "async function switchConsoleNavigation");
  for (const status of [401, 403, 500]) {
    let reject;
    let current = true;
    const pending = new Promise((_resolve, fail) => { reject = fail; });
    const effects = [];
    const authSession = { context: { current: true } };
    const scope = {
      authSession,
      readNavigationHashState: () => ({ value: "", rejected: false }),
      workspaceNavigationController: { loadNavigation: () => pending },
      clearAuthSession: () => effects.push("clear"),
      renderLogin: () => effects.push("login"),
      authErrorMessage: () => "session unavailable",
    };
    const load = runInNewContext(`${shellLoader}\nloadConsoleShellState;`, scope);
    const loading = load(() => current);
    current = false;
    reject({ status, requestId: "old-request" });
    assert.equal(await loading, null);
    assert.equal(scope.authSession, authSession);
    assert.deepEqual(effects, []);
  }
});

test("shell loader discards stale navigation success before changing the route or preferences", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const shellLoader = sourceSection(source, "async function loadConsoleShellState(",
    "async function switchConsoleNavigation");
  let resolve;
  let current = true;
  const pending = new Promise((accept) => { resolve = accept; });
  const scope = {
    readNavigationHashState: () => ({ value: "old-route", rejected: true }),
    workspaceNavigationController: { loadNavigation: () => pending },
    expandedConsoleGroupKey: "current-group",
  };
  const load = runInNewContext(`${shellLoader}\nloadConsoleShellState;`, scope);
  const loading = load(() => current);
  current = false;
  resolve({ navigation: { preference: { expanded_group_code: "old-group" } } });
  assert.equal(await loading, null);
  assert.equal(scope.expandedConsoleGroupKey, "current-group");
});

test("shell loader still clears a current 401 instead of suppressing authentication failure", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const shellLoader = sourceSection(source, "async function loadConsoleShellState(",
    "async function switchConsoleNavigation");
  const effects = [];
  const scope = {
    authSession: { context: { current: true } },
    readNavigationHashState: () => ({ value: "", rejected: false }),
    workspaceNavigationController: { loadNavigation: async () => { throw { status: 401 }; } },
    clearAuthSession: () => effects.push("clear"),
    renderLogin: () => effects.push("login"),
    authErrorMessage: () => "session unavailable",
  };
  const load = runInNewContext(`${shellLoader}\nloadConsoleShellState;`, scope);
  assert.equal(await load(() => true), null);
  assert.equal(scope.authSession.context, null);
  assert.deepEqual(effects, ["clear", "login"]);
});

test("workspace bootstrap retries one transient connection failure before showing the recovery page", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const shellLoader = sourceSection(source, "async function loadConsoleShellState(",
    "async function switchConsoleNavigation");
  assert.match(shellLoader, /allowTransientRetry = true/u);
  assert.match(shellLoader, /error\?\.code === "workspace_session\.required"/u);
  assert.match(shellLoader, /isRetryableWorkspaceBootstrapError\(error\)/u);
  assert.match(shellLoader, /return loadConsoleShellState\(isCurrent, false\)/u);
  assert.match(source, /function waitForWorkspaceBootstrapRetry\(\)/u);
});


test("navigation click hydrates only a backend-visible target before deferred telemetry", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const navigation = sourceSection(
    source,
    "async function switchConsoleNavigation",
    "function cancelPendingConsoleNavigationSideEffects",
  );
  const routeFenceIndex = navigation.indexOf("workspaceNavigationController.changeRoute();");
  const transitionIndex = navigation.indexOf("renderConsoleNavigationTransition(button");
  const authorizedItemIndex = navigation.indexOf("const projectedNavigationItem =", transitionIndex);
  const visibilityIndex = navigation.indexOf("item.visible !== false && item.disabled !== true", authorizedItemIndex);
  const renderIndex = navigation.indexOf("renderDeveloperStart(app", visibilityIndex);
  const bindIndex = navigation.indexOf("bindAuthenticatedEvents(authorizedProjectedState);", renderIndex);
  const hydrateIndex = navigation.indexOf("await loadAndRenderActiveNavigation({", bindIndex);
  const preserveSidebarIndex = navigation.indexOf("preserveConsoleSidebar: true", hydrateIndex);
  const sideEffectIndex = navigation.indexOf("scheduleConsoleNavigationSideEffects({", hydrateIndex);

  assert.ok(routeFenceIndex >= 0 && routeFenceIndex < transitionIndex);
  assert.ok(transitionIndex < authorizedItemIndex);
  assert.ok(authorizedItemIndex < visibilityIndex && visibilityIndex < renderIndex);
  assert.ok(renderIndex < bindIndex && bindIndex < hydrateIndex);
  assert.ok(hydrateIndex < preserveSidebarIndex && preserveSidebarIndex < sideEffectIndex);
  assert.doesNotMatch(navigation.slice(bindIndex, hydrateIndex), /getCurrentUser|loadConsoleShellState|refresh\(\)/);
  assert.match(source, /clearTimeout\(consoleNavigationSideEffectTimer\)/);
  assert.match(source, /CONSOLE_NAVIGATION_SIDE_EFFECT_DELAY_MS = 5_000/);
  const sideEffects = sourceSection(
    source,
    "function scheduleConsoleNavigationSideEffects",
    "function toggleConsoleSidebarGroup",
  );
  const canonicalPreferenceIndex = sideEffects.indexOf(
    "workspaceNavigationController.savePreference",
  );
  assert.ok(canonicalPreferenceIndex >= 0);
  assert.doesNotMatch(sideEffects, /updateConsolePreferences|recordConsolePageView|token/);
  assert.match(sideEffects, /if \(!navigationOwnerCurrent\(\)\) return;/);
});


test("active-page hydration preserves interactive sidebar DOM and refreshes handlers in place", async () => {
  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const renderSource = await readFile(
    new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url),
    "utf8",
  );
  const dispatcher = sourceSection(
    mainSource,
    "async function loadAndRenderActiveNavigation({",
    "function renderLogin(",
  );
  const binder = sourceSection(
    mainSource,
    "function bindAuthenticatedEvents(consoleShellState)",
    "async function switchConsoleNavigation",
  );

  assert.match(dispatcher, /preserveConsoleSidebar = false/);
  assert.match(dispatcher, /renderDeveloperStart\(app, \{[\s\S]*preserveConsoleSidebar,[\s\S]*\.\.\.state,/);
  assert.match(renderSource, /renderConsoleContentPreservingSidebar\(container, shellHtml, activeItem\.id\)/);
  assert.match(renderSource, /Keep its DOM identity[\s\S]*currentMain\.replaceWith\(nextMain\);/);
  assert.doesNotMatch(renderSource, /currentTopbar\.replaceWith\(nextTopbar\)/);
  assert.doesNotMatch(renderSource, /currentSidebar\.replaceWith/);
  assert.match(binder, /navigationButton\.onclick = async/);
  assert.match(binder, /groupToggle\.onclick =/);
  assert.match(binder, /sidebarToggle\.onclick = async/);
  assert.match(binder, /\.console-home-return, \.console-flow-next, \.console-flow-related/);
  assert.match(binder, /if \(event.ctrlKey \|\| event.metaKey \|\| event.shiftKey \|\| event.altKey\) return/);
});


test("sidebar groups use one persisted accordion expansion", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const toggle = sourceSection(source, "function toggleConsoleSidebarGroup", "/** 导航请求期间");
  assert.match(toggle, /document\.querySelectorAll\("\[data-sidebar-group\]"\)/);
  assert.match(toggle, /const shouldExpand = button\.getAttribute\("aria-expanded"\) !== "true"/);
  assert.match(toggle, /const next = shouldExpand \? key : null/);
  assert.match(toggle, /expandedConsoleGroupKey = next/);
  assert.match(toggle, /workspaceNavigationController\.savePreference\(\{[\s\S]*expanded_group_code: next/);
  assert.match(toggle, /catch \(error\)[\s\S]*expandedConsoleGroupKey = previous/);
  assert.match(toggle, /applyExpandedConsoleGroup\(previous\)/);
  assert.doesNotMatch(toggle, /localStorage|sessionStorage|persistExpandedConsoleGroupKey/);
});

test("developer-center expands from projected developer routes", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const toggleSource = sourceSection(source, "async function toggleConsoleSidebarGroup", "function applyExpandedConsoleGroup");
  const session = { context: { active: true } };
  const scope = {
    authSession: session,
    refreshSequence: 1,
    sidebarGroupSequence: 0,
    sidebarGroupPending: false,
    expandedConsoleGroupKey: "gateway",
    hasWorkspaceConsoleSession: (value) => value === session,
    sameWorkspaceConsoleSession: (left, right) => left === right,
    workspaceConsoleSessionFromShell: () => session,
    applyExpandedConsoleGroup: (key) => { scope.expandedConsoleGroupKey = key; },
    workspaceNavigationController: {
      snapshot: () => ({ navigation: { groups: [] }, advancedTools: null }),
      savePreference: async () => ({
        navigation: { preference: { expanded_group_code: "developer-center" } },
      }),
    },
    projectWorkspaceNavigationForConsole: (navigation) => navigation,
    handleConsoleWorkspaceContextInvalidation() {},
    renderConsoleSidebarPreferenceError() {},
  };
  const toggle = runInNewContext(`${toggleSource}\ntoggleConsoleSidebarGroup;`, scope);
  await toggle({
    dataset: { sidebarGroupToggle: "developer-center" },
    closest: () => ({ querySelector: () => ({}) }),
    getAttribute: () => "false",
  }, {
    workspaceNavigationContract: true,
    navigation: {
      groups: [],
      navigation_items: [{
        group_code: "developer-center",
        sidebar_visible: true,
        disabled: false,
      }],
    },
    workspaceContext: { role: "developer" },
  });
  assert.equal(scope.expandedConsoleGroupKey, "developer-center");
});

test("task links reveal their authorized destination group while hydration preserves manual exploration", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const navigation = sourceSection(source, "async function switchConsoleNavigation", "function cancelPendingConsoleNavigationSideEffects");
  assert.match(navigation, /if \(projectedNavigationItem\) \{[\s\S]*expandedConsoleGroupKey = projectedNavigationItem.group_code/);
  assert.match(navigation, /projectedNavigationItem\.sidebar_visible !== false[\s\S]*expandedConsoleGroupKey = null;[\s\S]*applyExpandedConsoleGroup\(null\)/);
  assert.match(navigation, /authorizedProjectedState = \{[\s\S]*sidebarExpandedGroupKey: expandedConsoleGroupKey/);
  assert.match(navigation, /bindAuthenticatedEvents\(authorizedProjectedState\);\s*revealConsoleSidebarItem\(app\)/);
  const transition = sourceSection(source, "function renderConsoleNavigationTransition", "async function toggleConsoleSidebar(");
  assert.match(transition, /const active = candidate.dataset.consoleNav === itemId/);
  const refresh = sourceSection(source, "async function performRefresh", "async function loadAndRenderActiveNavigation");
  assert.doesNotMatch(refresh, /expandedConsoleGroupKey = active\?\.group_code/);
  assert.match(refresh, /syncConsoleSidebarState\(consoleShellState\)/);
  const dispatcher = sourceSection(source, "async function loadAndRenderActiveNavigation", "function renderLogin(");
  assert.match(dispatcher, /if \(!isBackgroundRefresh && !preserveConsoleSidebar\) revealConsoleSidebarItem\(app\)/);
  assert.match(dispatcher, /case "virtual-keys":[\s\S]*state.workspaceTaskHomeState\] = await Promise.all\(\[[\s\S]*loadWorkspaceConnectionProgress\(\)/);
});

test("empty entry content is replaced before navigation and resource network waits", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const refresh = sourceSection(source, "async function performRefresh", "async function loadAndRenderActiveNavigation");
  assert.match(refresh, /if \(!app\?\.firstElementChild\) renderConsoleEntryLoading\(app\);\s*const consoleShellState = await loadConsoleShellState/);
  const dispatcher = sourceSection(source, "async function loadAndRenderActiveNavigation", "function renderLogin(");
  assert.ok(dispatcher.indexOf("renderConsoleEntryLoading(app)") < dispatcher.indexOf("const state = {}"));
  assert.ok(dispatcher.indexOf("if (!refreshOwnerCurrent()") < dispatcher.indexOf("renderConsoleEntryLoading(app)"));
});


test("Console bootstrap never reads Legacy credentials or offers a second password login", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const startup = sourceSection(source, "async function start()", "async function refresh(");
  assert.match(startup, /clearAuthSession\(\);\s*await refresh\(\);/);
  assert.doesNotMatch(source, /readAuthSession|saveAuthSession|getCurrentUser|loginAdmin|logoutAdmin|loginGeneration/);
  const prompt = sourceSection(source, "function renderLogin(", "async function loadWorkspaceConnectionProgress(");
  assert.match(prompt, /window\.location\.replace\(identityLoginHref\(\{ interrupted: Boolean\(message\), requestId \}\)\)/);
  assert.doesNotMatch(prompt, /renderWorkspaceSessionPrompt/);
  assert.doesNotMatch(prompt, /FormData|password|submit/);
});


test("shared cleanup fences all existing page state families", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const cleanup = sourceSection(source, "function clearConsoleSessionState(", "function invitationOwnerInput(");
  for (const call of ["clearEnterpriseChatSessionState", "clearPromptTemplateSessionState",
    "clearPolicyTemplateLibrarySessionState", "clearWebhookSessionState", "clearUsageExportSessionState",
    "clearAuditExportSessionState", "clearBudgetSessionState", "clearPolicyEditorSessionState",
    "clearPolicySnapshotSessionState", "clearPolicyDryRunSessionState", "clearIntentSecuritySessionState",
    "clearRoutingPolicySessionState", "clearModelQualityScoreSessionState", "clearRequestLogSessionState",
    "clearRequestExplanationSessionState", "clearRequestFeedbackSummarySessionState"]) {
    assert.ok(cleanup.includes(`${call}();`), call);
  }
  assert.match(cleanup, /if \(clearNavigation\) workspaceNavigationController\.leave\(\)/);
  assert.match(cleanup, /workspaceConsoleExperienceController\.leave\(\)/);
});


test("login redirect invalidates refresh ownership before cleanup and navigation", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const login = sourceSection(source, "function renderLogin(", "function clearConsoleSessionState(");
  const invalidateIndex = login.indexOf("refreshSequence += 1;");
  const cleanupIndex = login.indexOf("clearConsoleSessionState();");
  const renderIndex = login.indexOf('window.location.replace(identityLoginHref(');
  assert.ok(invalidateIndex >= 0 && invalidateIndex < cleanupIndex && cleanupIndex < renderIndex);
});
