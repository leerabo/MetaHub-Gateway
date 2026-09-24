import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";

const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");

function section(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from);
  return source.slice(from, to);
}

function deferred() {
  let resolve;
  const promise = new Promise((accept) => { resolve = accept; });
  return { promise, resolve };
}

test("refresh adopts server expansion only before a newer local choice or pending save", async () => {
  const prefix = section("async function performRefresh(options = {})", "  const previousResourceNavigation =");
  for (const scenario of ["server", "new-choice", "pending-before", "pending-during", "new-session"]) {
    const session = {};
    const pending = deferred();
    const scope = {
      consoleLogoutPending: false, sidebarGroupSequence: 5,
      sidebarGroupPending: scenario === "pending-before" || scenario === "new-session",
      expandedConsoleGroupKey: "workspace", refreshSequence: 0, nextRefreshIsBackground: false,
      resourcePageLifecycle: { cancelReads() {} }, filePageState: { cancelList() {} },
      clearDeveloperContractSecurityLoad() {}, workspaceProfileMutation: null,
      workspaceProfileRecovery: null, authSession: session,
      isConsoleRefreshOwnerCurrent: () => true, app: { firstElementChild: {} },
      loadConsoleShellState: () => pending.promise,
      workspaceConsoleSessionFromShell: () => scenario === "new-session" ? {} : session,
      sameWorkspaceConsoleSession: (left, right) => left === right,
      clearConsoleSessionState() {},
    };
    scope.app.replaceChildren = () => {};
    const refresh = runInNewContext(`${prefix}\nreturn consoleShellState;\n}\nperformRefresh;`, scope);
    const loading = refresh();
    if (scenario === "new-choice") scope.sidebarGroupSequence += 1;
    if (scenario === "pending-before") scope.sidebarGroupPending = false;
    if (scenario === "pending-during") scope.sidebarGroupPending = true;
    pending.resolve({ sidebarExpandedGroupKey: "finance" });
    await loading;
    assert.equal(scope.expandedConsoleGroupKey,
      ["server", "new-session"].includes(scenario) ? "finance" : "workspace", scenario);
  }
});

test("late page hydration uses current expansion and mobile state, pruning hidden groups", () => {
  const scope = { expandedConsoleGroupKey: "workspace", mobileSidebarCollapsed: true };
  const sync = runInNewContext(`${section("function syncConsoleSidebarState(", "function applyExpandedConsoleGroup(")}\nsyncConsoleSidebarState;`, scope);
  const shell = {
    sidebarExpandedGroupKey: "finance", mobileSidebarCollapsed: false,
    navigation: { navigation_items: [{ group_code: "workspace", visible: true }] },
  };
  sync(shell);
  assert.equal(shell.sidebarExpandedGroupKey, "workspace");
  assert.equal(shell.mobileSidebarCollapsed, true);
  shell.navigation.navigation_items[0].visible = false;
  sync(shell);
  assert.equal(shell.sidebarExpandedGroupKey, null);
  assert.equal(scope.expandedConsoleGroupKey, null);
});

test("a delayed page preference response cannot replace a later group choice", async () => {
  const pending = deferred();
  let callback;
  const scope = {
    cancelPendingConsoleNavigationSideEffects() {}, consoleNavigationSideEffectTimer: null,
    setTimeout(fn) { callback = fn; return 1; }, CONSOLE_NAVIGATION_SIDE_EFFECT_DELAY_MS: 5000,
    WORKSPACE_ROUTE_REGISTRY: { finance: { placement: "primary" } },
    workspaceNavigationController: { savePreference: () => pending.promise },
    expandedConsoleGroupKey: "finance", sidebarGroupSequence: 1, sidebarGroupPending: false,
  };
  const schedule = runInNewContext(`${section("function scheduleConsoleNavigationSideEffects(", "/** 分组手风琴")}\nscheduleConsoleNavigationSideEffects;`, scope);
  schedule({ routeCode: "finance", navigationOwnerCurrent: () => true });
  const saving = callback();
  scope.expandedConsoleGroupKey = "developer-center";
  scope.sidebarGroupSequence += 1;
  pending.resolve({ navigation: { preference: { expanded_group_code: "finance" } } });
  await saving;
  assert.equal(scope.expandedConsoleGroupKey, "developer-center");
});

test("authorized page selection closes mobile navigation without changing desktop preference", () => {
  const start = "  if (projectedNavigationItem) {";
  const from = source.indexOf(start, source.indexOf("async function switchConsoleNavigation"));
  const to = source.indexOf("    renderDeveloperStart(app,", from);
  assert.ok(from >= 0 && to > from);
  const scope = {
    projectedNavigationItem: { group_code: "developer-center" },
    consoleShellState: { preferences: { preference: { sidebar_collapsed: false } }, navigation: {} },
    mobileSidebarCollapsed: false, expandedConsoleGroupKey: "finance",
    authorizedProjectedState: null, itemId: "sdk-examples", routeCode: "workspace.developer.sdk",
  };
  runInNewContext(`${source.slice(from, to)}\n}`, scope);
  assert.equal(scope.mobileSidebarCollapsed, true);
  assert.equal(scope.authorizedProjectedState.mobileSidebarCollapsed, true);
  assert.equal(scope.authorizedProjectedState.preferences.preference.sidebar_collapsed, false);
});
