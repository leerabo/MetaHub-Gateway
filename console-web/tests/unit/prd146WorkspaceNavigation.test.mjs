import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderConsoleShell } from "../../src/features/console-shell/renderConsoleShell.js";
import {
  renderWorkspaceAdvancedTools,
  renderWorkspaceTaskHome,
} from "../../src/features/workspace-navigation/renderWorkspaceNavigation.js";
import {
  createWorkspaceNavigationController,
} from "../../src/features/workspace-navigation/workspaceNavigationController.js";
import {
  createWorkspaceNavigationStore,
  workspaceNavigationOwner,
} from "../../src/features/workspace-navigation/workspaceNavigationStore.js";
import {
  WORKSPACE_ROUTE_REGISTRY,
  WORKSPACE_SOURCE_ROUTE_REGISTRY,
  getWorkspaceAdvancedTools,
  getWorkspaceGettingStarted,
  getWorkspaceNavigation,
  normalizeWorkspaceAdvancedToolOpen,
  normalizeWorkspaceAdvancedTools,
  normalizeWorkspaceGettingStarted,
  normalizeWorkspaceNavigation,
  normalizeWorkspaceNavigationPreference,
  openWorkspaceAdvancedTool,
  projectWorkspaceNavigationForConsole,
  resolveWorkspaceNavigationRoute,
  updateWorkspaceNavigationPreference,
} from "../../src/lib/workspaceNavigation.js";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ONE = `wsp_${"1".repeat(32)}`;
const WORKSPACE_TWO = `wsp_${"2".repeat(32)}`;
const SNAPSHOT = `ncs_${"3".repeat(32)}`;
const GLOBAL_CSRF = "g".repeat(43);
const WORKSPACE_CSRF = "w".repeat(43);
const FINGERPRINT = "f".repeat(64);

test("PRD-146 clients use strict Cookie DTOs, private headers, CAS, and no bearer", async () => {
  const originalDocument = globalThis.document;
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.document = {
    cookie: [
      `__Host-metahub-global-csrf=${GLOBAL_CSRF}`,
      `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}`,
    ].join("; "),
  };
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    const path = new URL(url).pathname;
    if (path === "/workspace/navigation") {
      return privateResponse(navigationPayload(), '"4.6.8.10"');
    }
    if (path === "/workspace/getting-started") {
      return privateResponse(taskHomePayload(), '"4.3.8.10"');
    }
    if (path === "/workspace/advanced-tools") {
      return privateResponse(advancedToolsPayload(), '"4.5.8.10"');
    }
    if (path === "/identity/preferences/navigation") {
      return privateResponse(preferencePayload(), '"3"');
    }
    if (path === "/workspace/advanced-tools/provider_error_overrides/open") {
      return privateResponse(advancedOpenPayload(), '"7"');
    }
    throw new Error(`unexpected path ${path}`);
  };

  try {
    const navigation = await getWorkspaceNavigation({ controlApiUrl: CONTROL_URL });
    const tasks = await getWorkspaceGettingStarted({ controlApiUrl: CONTROL_URL });
    const tools = await getWorkspaceAdvancedTools({ controlApiUrl: CONTROL_URL });
    const preference = await updateWorkspaceNavigationPreference({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ONE,
      contractSnapshotId: SNAPSHOT,
      revision: 2,
      changes: { expanded_group_code: "gateway" },
      idempotencyKey: "preference:0123456789abcdef",
    });
    const opened = await openWorkspaceAdvancedTool({
      controlApiUrl: CONTROL_URL,
      toolCode: "provider_error_overrides",
      entitlementRevision: 7,
      idempotencyKey: "advanced-open:0123456789abcdef",
    });

    assert.equal(navigation.groups.length, 8);
    assert.equal(tasks.tasks[0].action_route_code, "workspace.gateway.providers");
    assert.equal(tools.tools[0].tool_code, "provider_error_overrides");
    assert.equal(preference.owner_ref, undefined);
    assert.equal(preference.observed_at, undefined);
    assert.equal(opened.observed_at, undefined);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  }

  assert.deepEqual(
    calls.map(({ url, options }) => [
      new URL(url).pathname,
      options.method,
      options.credentials,
      options.cache,
    ]),
    [
      ["/workspace/navigation", "GET", "include", "no-store"],
      ["/workspace/getting-started", "GET", "include", "no-store"],
      ["/workspace/advanced-tools", "GET", "include", "no-store"],
      ["/identity/preferences/navigation", "PATCH", "include", "no-store"],
      ["/workspace/advanced-tools/provider_error_overrides/open", "POST", "include", "no-store"],
    ],
  );
  for (const call of calls) {
    assert.equal("Authorization" in call.options.headers, false);
  }
  assert.equal(calls[3].options.headers["X-CSRF-Token"], GLOBAL_CSRF);
  assert.equal(calls[3].options.headers["If-Match"], '"2"');
  assert.equal(calls[4].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[4].options.headers["If-Match"], '"7"');
  assert.equal(calls[4].options.body, undefined);
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    workspace_id: WORKSPACE_ONE,
    contract_snapshot_id: SNAPSHOT,
    expanded_group_code: "gateway",
  });
});

test("PRD-146 normalizers reject unknown fields and mismatched private evidence", async () => {
  assert.throws(
    () => normalizeWorkspaceNavigation({ ...navigationPayload(), permission_codes: [] }),
    (error) => error.code === "workspace_navigation.response_invalid",
  );
  assert.throws(
    () => normalizeWorkspaceAdvancedTools({
      ...advancedToolsPayload(),
      tools: [{
        ...advancedToolsPayload().tools[0],
        route_code: "workspace.gateway.providers",
      }],
    }),
    (error) => error.code === "workspace_navigation.response_invalid",
  );
  assert.throws(
    () => normalizeWorkspaceGettingStarted({
      ...taskHomePayload(),
      tasks: [{ ...taskHomePayload().tasks[0], status: "completed" }],
    }),
    (error) => error.code === "workspace_navigation.response_invalid",
  );
  assert.throws(
    () => normalizeWorkspaceGettingStarted({
      ...taskHomePayload(),
      completed_count: 1,
    }),
    (error) => error.code === "workspace_navigation.response_invalid",
  );
  assert.throws(
    () => normalizeWorkspaceAdvancedTools({
      ...advancedToolsPayload(),
      tools: [{
        ...advancedToolsPayload().tools[0],
        availability: "blocked",
        reason_code: "deployment.required",
      }],
    }),
    (error) => error.code === "workspace_navigation.response_invalid",
  );
  assert.doesNotThrow(() => normalizeWorkspaceNavigationPreference(preferencePayload()));
  assert.doesNotThrow(() => normalizeWorkspaceAdvancedToolOpen(advancedOpenPayload()));

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => privateResponse(
    navigationPayload(),
    '"4.6.8.10"',
    { "X-Permission-Fingerprint": "0".repeat(64) },
  );
  try {
    await assert.rejects(
      getWorkspaceNavigation({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "workspace_navigation.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("PRD-146 canonical registry restores five sources and excludes six retired sources", () => {
  const restored = [
    "providers",
    "local-models",
    "provider-health",
    "error-maps",
    "billing-disputes",
  ];
  const retired = [
    "growth-analytics",
    "customer-health",
    "partner-portal",
    "adapter-specs",
    "adapter-contract-runs",
    "competitor-comparison",
  ];
  for (const sourceId of restored) {
    const routeCode = WORKSPACE_SOURCE_ROUTE_REGISTRY[sourceId];
    assert.ok(routeCode, `missing restored source ${sourceId}`);
    assert.equal(WORKSPACE_ROUTE_REGISTRY[routeCode].sourceId, sourceId);
  }
  for (const sourceId of retired) {
    assert.equal(WORKSPACE_SOURCE_ROUTE_REGISTRY[sourceId], undefined);
  }
  assert.equal(
    Object.keys(WORKSPACE_ROUTE_REGISTRY).some((route) => route.includes(".retired.")),
    false,
  );

  const navigation = normalizeWorkspaceNavigation(navigationPayload());
  const resolution = resolveWorkspaceNavigationRoute(
    navigation,
    "workspace.retired.partner-portal",
  );
  assert.deepEqual(resolution, {
    routeCode: "workspace.home.overview",
    rejected: true,
  });
});

test("platform internal and incomplete workflows are never Workspace sidebar or direct-link targets", () => {
  assert.equal(WORKSPACE_ROUTE_REGISTRY["workspace.settings.deployments"].placement, "platform_internal");
  assert.equal(WORKSPACE_ROUTE_REGISTRY["workspace.settings.data-management"].placement, "platform_internal");
  const payload = navigationPayload();
  payload.groups.find((group) => group.group_code === "home").items.push(
    item("workspace.home.approvals", "primary", 12),
  );
  const navigation = normalizeWorkspaceNavigation(payload);
  for (const routeCode of [
    "workspace.settings.deployments",
    "workspace.settings.data-management",
    "workspace.home.assistant",
    "workspace.home.approvals",
  ]) {
    assert.deepEqual(
      resolveWorkspaceNavigationRoute(navigation, routeCode),
      { routeCode: "workspace.home.overview", rejected: true },
    );
  }
  const projected = projectWorkspaceNavigationForConsole(
    navigation,
    "workspace.home.overview",
    null,
    { permission_codes: ["tenant_data.read", "tenant.settings.read"] },
    "owner",
  );
  assert.equal(
    projected.navigation_items.some((item) => ["environments", "tenant-data-lifecycle"].includes(item.id)),
    false,
  );
  assert.equal(projected.navigation_items.some((item) => item.id === "admin-assistant"), false);
  assert.equal(projected.navigation_items.some((item) => item.id === "approvals"), false);
});

test("governance navigation presents templates before rules and reserves publishing for publishers", () => {
  const payload = navigationPayload();
  const governance = payload.groups.find((group) => group.group_code === "governance");
  governance.items.push(
    item("workspace.governance.policy-releases", "primary", 92),
    item("workspace.governance.policy-templates", "primary", 93),
  );
  const navigation = normalizeWorkspaceNavigation(payload);
  const reader = { permission_codes: ["policy.read", "policy_template.read"] };
  assert.equal(resolveWorkspaceNavigationRoute(navigation, "workspace.governance.policy-releases",
    { authorization: reader }).rejected, true);
  const limited = projectWorkspaceNavigationForConsole(
    navigation, "workspace.home.overview", null, reader, "owner",
  );
  assert.equal(limited.navigation_items.some((entry) => entry.id === "policy-snapshots"), false);
  const publisher = projectWorkspaceNavigationForConsole(
    navigation, "workspace.home.overview", null,
    { permission_codes: [...reader.permission_codes, "policy.publish"] }, "owner",
  );
  const policyItems = publisher.navigation_items
    .filter((entry) => ["policy-templates", "policies", "policy-snapshots"].includes(entry.id))
    .sort((left, right) => left.order - right.order)
    .map((entry) => entry.id);
  assert.deepEqual(policyItems, ["policy-templates", "policies", "policy-snapshots"]);
});

test("PRD-146 store supersedes lane tickets and changes route without losing navigation", () => {
  const store = createWorkspaceNavigationStore({
    createIdempotencyKey: () => "preference:test-key",
  });
  assert.equal(store.enter(workspaceNavigationOwner(identitySession(), workspaceSession())), true);
  const navigation = normalizeWorkspaceNavigation(navigationPayload());
  const navigationTicket = store.begin("navigation");
  assert.equal(store.accept(navigationTicket, navigation), true);

  const staleTask = store.begin("task-home");
  const currentTask = store.begin("task-home");
  assert.equal(staleTask.controller.signal.aborted, true);
  assert.equal(store.accept(staleTask, normalizeWorkspaceGettingStarted(taskHomePayload())), false);
  assert.equal(
    store.accept(currentTask, normalizeWorkspaceGettingStarted(taskHomePayload())),
    true,
  );

  const advancedTicket = store.begin("advanced-tools");
  const generation = store.snapshot().generation;
  store.changeRoute();
  assert.equal(advancedTicket.controller.signal.aborted, true);
  assert.equal(store.snapshot().generation, generation + 1);
  assert.equal(store.snapshot().navigation, navigation);
  assert.equal(store.snapshot().taskHome, null);
  assert.equal(store.snapshot().advancedTools, null);
  assert.equal(store.snapshot().busy, false);

  const cancellableAdvanced = store.begin("advanced-tools");
  assert.equal(store.cancel("advanced-tools"), true);
  assert.equal(cancellableAdvanced.controller.signal.aborted, true);
  assert.equal(store.snapshot().navigation, navigation);
  assert.equal(store.snapshot().busy, false);

  store.leave();
  assert.equal(store.snapshot().navigation, null);
  assert.equal(store.snapshot().owner, null);
});

test("PRD-146 store accepts only exact changed and unchanged preference revisions", () => {
  const store = createWorkspaceNavigationStore({
    createIdempotencyKey: () => "preference:test-key",
  });
  assert.equal(store.enter(workspaceNavigationOwner(identitySession(), workspaceSession())), true);
  const navigation = normalizeWorkspaceNavigation(navigationPayload());
  assert.equal(store.accept(store.begin("navigation"), navigation), true);

  const unchanged = store.beginPreference({ expanded_group_code: null });
  assert.equal(store.acceptPreference(
    unchanged,
    normalizeWorkspaceNavigationPreference({
      ...preferencePayload(),
      changed: false,
      revision: 2,
      expanded_group_code: null,
    }),
  ), true);
  assert.equal(store.snapshot().navigation.preference.revision, 2);

  const invalid = store.beginPreference({ sidebar_collapsed: false });
  assert.equal(store.acceptPreference(
    invalid,
    normalizeWorkspaceNavigationPreference({
      ...preferencePayload(),
      changed: true,
      revision: 2,
      expanded_group_code: null,
    }),
  ), false);
  assert.equal(store.snapshot().status, "conflict");
});

test("PRD-146 controller retries preference and advanced open with original idempotency keys", async () => {
  let preferenceAttempts = 0;
  let advancedAttempts = 0;
  const preferenceKeys = [];
  const advancedKeys = [];
  let keySequence = 0;
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: (namespace) => `${namespace}:key-${++keySequence}`,
    api: completeApi({
      updatePreference: async (input) => {
        preferenceKeys.push(input.idempotencyKey);
        preferenceAttempts += 1;
        if (preferenceAttempts === 1) throw Object.assign(new Error("temporary"), { status: 503 });
        return normalizeWorkspaceNavigationPreference(preferencePayload());
      },
      openAdvancedTool: async (input) => {
        advancedKeys.push(input.idempotencyKey);
        advancedAttempts += 1;
        if (advancedAttempts === 1) throw Object.assign(new Error("temporary"), { status: 503 });
        return normalizeWorkspaceAdvancedToolOpen(advancedOpenPayload());
      },
    }),
  });

  await controller.loadNavigation();
  await assert.rejects(
    controller.savePreference({ expanded_group_code: "gateway" }),
    /temporary/u,
  );
  assert.equal(controller.snapshot().navigation.preference.expanded_group_code, null);
  await controller.savePreference({ expanded_group_code: "gateway" });
  assert.deepEqual(preferenceKeys, [preferenceKeys[0], preferenceKeys[0]]);
  assert.equal(controller.snapshot().navigation.preference.expanded_group_code, "gateway");

  await controller.loadAdvancedTools();
  await assert.rejects(controller.openAdvancedTool("provider_error_overrides"), /temporary/u);
  await controller.openAdvancedTool("provider_error_overrides");
  assert.deepEqual(advancedKeys, [advancedKeys[0], advancedKeys[0]]);
});

test("PRD-146 controller establishes the only Workspace after a fresh login", async () => {
  const calls = [];
  let firstSession = true;
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: (namespace) => `${namespace}:fresh-login`,
    api: completeApi({
      workspaceSession: async () => {
        calls.push("session");
        if (firstSession) {
          firstSession = false;
          throw Object.assign(new Error("workspace required"), {
            code: "workspace_session.required", status: 401,
          });
        }
        return workspaceSession();
      },
      workspaces: async () => {
        calls.push("workspaces");
        return { workspaces: [{ workspace_id: WORKSPACE_ONE, role: "owner" }] };
      },
      switchWorkspace: async ({ workspaceId }) => {
        calls.push(["switch", workspaceId]);
        return { context: workspaceSession(workspaceId).context };
      },
    }),
  });

  const loaded = await controller.loadNavigation();
  assert.equal(loaded.context.workspace_id, WORKSPACE_ONE);
  assert.deepEqual(calls, ["session", "workspaces", ["switch", WORKSPACE_ONE], "session"]);
});

test("PRD-146 route changes abort in-flight preference and fence queued writes", async () => {
  const pending = deferred();
  const calls = [];
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: (namespace) => `${namespace}:stable-key`,
    api: completeApi({
      updatePreference: async (input) => {
        calls.push(input);
        return pending.promise;
      },
    }),
  });
  await controller.loadNavigation();
  const first = controller.savePreference({ expanded_group_code: "gateway" });
  const second = controller.savePreference({ sidebar_collapsed: true });
  const outcomes = [first, second].map((promise) => promise.then(
    () => ({ status: "fulfilled" }),
    (error) => ({ status: "rejected", error }),
  ));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 1);

  controller.changeRoute();
  assert.equal(calls[0].signal.aborted, true);
  pending.resolve(normalizeWorkspaceNavigationPreference(preferencePayload()));
  const settled = await Promise.all(outcomes);
  assert.deepEqual(settled.map((item) => item.status), ["rejected", "rejected"]);
  assert.equal(calls.length, 1);
  assert.equal(controller.snapshot().navigation.preference.expanded_group_code, null);
  assert.equal(controller.snapshot().navigation.preference.sidebar_collapsed, false);
});

test("PRD-146 late WorkspaceSession cannot replace the newer Workspace owner", async () => {
  const sessions = [deferred(), deferred()];
  const signals = [];
  let sessionIndex = 0;
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: (namespace) => `${namespace}:stable-key`,
    api: completeApi({
      workspaceSession: ({ signal }) => {
        signals.push(signal);
        return sessions[sessionIndex++].promise;
      },
      navigation: async () => normalizeWorkspaceNavigation(navigationPayload()),
    }),
  });

  const first = controller.loadNavigation().then(
    (value) => ({ value }),
    (error) => ({ error }),
  );
  await new Promise((resolve) => setImmediate(resolve));
  const second = controller.loadNavigation();
  assert.equal(signals[0].aborted, true);
  await new Promise((resolve) => setImmediate(resolve));
  sessions[1].resolve(workspaceSession(WORKSPACE_TWO, "admin"));
  const current = await second;
  sessions[0].resolve(workspaceSession(WORKSPACE_ONE, "owner"));
  const stale = await first;

  assert.equal(current.context.workspace_id, WORKSPACE_TWO);
  assert.equal(controller.snapshot().owner.workspaceId, WORKSPACE_TWO);
  assert.equal(stale.error.status, 409);
});

test("PRD-146 task home and advanced tools load only through their explicit routes", async () => {
  const calls = { navigation: 0, tasks: 0, advanced: 0 };
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: (namespace) => `${namespace}:stable-key`,
    api: completeApi({
      navigation: async () => {
        calls.navigation += 1;
        return normalizeWorkspaceNavigation(navigationPayload());
      },
      gettingStarted: async () => {
        calls.tasks += 1;
        return normalizeWorkspaceGettingStarted(taskHomePayload());
      },
      advancedTools: async () => {
        calls.advanced += 1;
        return normalizeWorkspaceAdvancedTools(advancedToolsPayload());
      },
    }),
  });

  await controller.loadNavigation();
  assert.deepEqual(calls, { navigation: 1, tasks: 0, advanced: 0 });
  await controller.loadGettingStarted();
  assert.deepEqual(calls, { navigation: 1, tasks: 1, advanced: 0 });
  controller.changeRoute();
  assert.equal(controller.snapshot().taskHome, null);
  await controller.loadAdvancedTools();
  assert.deepEqual(calls, { navigation: 1, tasks: 1, advanced: 1 });
});

test("PRD-146 navigation binds the actual GlobalSession and Workspace authorization", async () => {
  let identityReads = 0;
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: () => "preference:stable-key",
    api: completeApi({ identitySession: async () => {
      identityReads += 1;
      return identitySession();
    } }),
  });
  const loaded = await controller.loadNavigation();
  assert.equal(identityReads, 1);
  assert.equal(loaded.owner.globalSessionId, "identity-session-one");
  assert.equal(loaded.owner.permissionFingerprint, FINGERPRINT);
  assert.equal(loaded.owner.membershipRevision, 9);
  assert.equal(loaded.identity.current_session.session_id, "identity-session-one");
  assert.equal(loaded.authorization.permission_revision, 10);
});

test("PRD-146 a new navigation load fences old bytes while new authentication is pending", async () => {
  const oldNavigation = deferred();
  const nextIdentity = deferred();
  let identityCalls = 0;
  let navigationCalls = 0;
  let oldSignal;
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: () => "preference:stable-key",
    api: completeApi({
      identitySession: () => ++identityCalls === 1 ? identitySession() : nextIdentity.promise,
      navigation: ({ signal }) => {
        if (++navigationCalls !== 1) return normalizeWorkspaceNavigation(navigationPayload());
        oldSignal = signal;
        return oldNavigation.promise;
      },
    }),
  });
  const first = controller.loadNavigation().catch((error) => error);
  await new Promise((resolve) => setImmediate(resolve));
  const second = controller.loadNavigation();
  assert.equal(oldSignal.aborted, true);
  oldNavigation.resolve(normalizeWorkspaceNavigation(navigationPayload()));
  assert.equal((await first).status, 409);
  assert.equal(controller.snapshot().navigation, null);
  nextIdentity.resolve(identitySession());
  assert.equal((await second).status, "ready");
});

for (const field of ["globalSessionId", "revokeGeneration", "globalExpiresAt", "workspaceExpiresAt",
  "permissionRevision", "membershipRevision", "permissionFingerprint"]) {
  test(`PRD-146 changing owner ${field} invalidates every outstanding lane`, () => {
    const store = createWorkspaceNavigationStore({ createIdempotencyKey: () => "preference:stable-key" });
    const owner = workspaceNavigationOwner(identitySession(), workspaceSession());
    assert.equal(store.enter(owner), true);
    assert.equal(store.accept(store.begin("navigation"), normalizeWorkspaceNavigation(navigationPayload())), true);
    const tickets = ["task-home", "advanced-tools", "advanced-open"].map(store.begin);
    tickets.push(store.beginPreference({ sidebar_collapsed: true }));
    const replacements = {
      globalSessionId: "identity-session-two", revokeGeneration: 2,
      globalExpiresAt: "2026-09-10T00:00:00Z", workspaceExpiresAt: "2026-09-10T00:00:00Z",
      permissionRevision: 11, membershipRevision: 10, permissionFingerprint: "e".repeat(64),
    };
    assert.equal(store.enter({ ...owner, [field]: replacements[field] }), true);
    for (const ticket of tickets) assert.equal(ticket.controller.signal.aborted, true);
    assert.equal(store.snapshot().navigation, null);
    assert.equal(store.snapshot().taskHome, null);
    assert.equal(store.snapshot().advancedTools, null);
  });
}

test("PRD-146 malformed or inactive session owners cannot reuse an earlier navigation", async () => {
  for (const change of [
    (identity) => { identity.account.status = "disabled"; },
    (identity) => { identity.current_session.is_current = false; },
    (identity) => { delete identity.current_session.session_id; },
    (_identity, session) => { session.context.role = "service_account"; },
    (_identity, session) => { session.context.session_status = "revoked"; },
    (_identity, session) => { delete session.authorization; },
  ]) {
    const identity = identitySession();
    const session = workspaceSession();
    const controller = createWorkspaceNavigationController({
      createIdempotencyKey: () => "preference:stable-key",
      api: completeApi({ identitySession: async () => identity, workspaceSession: async () => session }),
    });
    await controller.loadNavigation();
    change(identity, session);
    await assert.rejects(controller.loadNavigation(), TypeError);
    assert.equal(controller.snapshot().navigation, null);
    assert.equal(controller.snapshot().owner, null);
  }
});

test("PRD-146 new navigation epochs retire old task success and failure without poisoning ready state", () => {
  const store = createWorkspaceNavigationStore({ createIdempotencyKey: () => "preference:stable-key" });
  store.enter(workspaceNavigationOwner(identitySession(), workspaceSession()));
  store.accept(store.begin("navigation"), normalizeWorkspaceNavigation(navigationPayload()));
  const task = store.begin("task-home");
  const advanced = store.begin("advanced-tools");
  const navigation = normalizeWorkspaceNavigation({ ...navigationPayload(), navigation_epoch: 9 });
  assert.equal(store.accept(store.begin("navigation"), navigation), true);
  assert.equal(task.controller.signal.aborted, true);
  assert.equal(advanced.controller.signal.aborted, true);
  assert.equal(store.accept(task, normalizeWorkspaceGettingStarted(taskHomePayload())), false);
  assert.equal(store.reject(advanced, { status: 401 }), false);
  assert.equal(store.snapshot().navigation, navigation);
  assert.equal(store.snapshot().status, "ready");
  assert.equal(store.snapshot().error, null);
});

test("PRD-146 a same-role GlobalSession replacement aborts old private lanes", async () => {
  let identity = identitySession();
  const pending = deferred();
  let signal;
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: () => "preference:stable-key",
    api: completeApi({
      identitySession: async () => identity,
      gettingStarted: (input) => { signal = input.signal; return pending.promise; },
    }),
  });
  await controller.loadNavigation();
  const old = controller.loadGettingStarted().catch((error) => error);
  identity = identitySession("identity-session-two");
  await controller.loadNavigation();
  assert.equal(signal.aborted, true);
  pending.resolve(normalizeWorkspaceGettingStarted(taskHomePayload()));
  assert.equal((await old).status, 409);
  assert.equal(controller.snapshot().taskHome, null);
  assert.equal(controller.snapshot().owner.globalSessionId, "identity-session-two");
});

test("PRD-146 stale preference success cannot poison a newer ready snapshot", async () => {
  const pending = deferred();
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: () => "preference:stable-key",
    api: completeApi({ updatePreference: () => pending.promise }),
  });
  await controller.loadNavigation();
  const saving = controller.savePreference({ expanded_group_code: "gateway" }).catch((error) => error);
  await new Promise((resolve) => setImmediate(resolve));
  controller.changeRoute();
  await controller.loadNavigation();
  pending.resolve(normalizeWorkspaceNavigationPreference(preferencePayload()));
  assert.equal((await saving).status, 409);
  assert.equal(controller.snapshot().status, "ready");
  assert.equal(controller.snapshot().error, null);
  assert.equal(controller.snapshot().navigation.preference.expanded_group_code, null);
});

for (const status of [401, 403]) {
  test(`PRD-146 superseded session ${status} is a conflict, not a new authentication failure`, async () => {
    const pending = deferred();
    let calls = 0;
    const controller = createWorkspaceNavigationController({
      createIdempotencyKey: () => "preference:stable-key",
      api: completeApi({ workspaceSession: () => ++calls === 1 ? pending.promise : workspaceSession() }),
    });
    const old = controller.loadNavigation().catch((error) => error);
    await new Promise((resolve) => setImmediate(resolve));
    await controller.loadNavigation();
    pending.reject(Object.assign(new Error("old session failed"), { status }));
    assert.equal((await old).status, 409);
    assert.equal(controller.snapshot().status, "ready");
  });

  test(`PRD-146 current task ${status} clears protected navigation and actions`, async () => {
    const controller = createWorkspaceNavigationController({
      createIdempotencyKey: () => "preference:stable-key",
      api: completeApi({ gettingStarted: async () => {
        throw Object.assign(new Error("permission no longer available"), { status });
      } }),
    });
    await controller.loadNavigation();
    await controller.loadAdvancedTools();
    await assert.rejects(controller.loadGettingStarted(), (error) => error.status === status);
    assert.equal(controller.snapshot().navigation, null);
    assert.equal(controller.snapshot().advancedTools, null);
    assert.equal(controller.snapshot().busy, false);
  });
}

for (const field of ["permission_fingerprint", "permission_revision", "membership_revision"]) {
  test(`PRD-146 navigation rejects WorkspaceSession ${field} mismatch`, async () => {
    const session = workspaceSession();
    session.authorization[field] = field.endsWith("fingerprint") ? "e".repeat(64) : 99;
    const controller = createWorkspaceNavigationController({
      createIdempotencyKey: () => "preference:stable-key",
      api: completeApi({ workspaceSession: async () => session }),
    });
    await assert.rejects(controller.loadNavigation(), (error) => error.status === 409);
    assert.equal(controller.snapshot().navigation, null);
    assert.equal(controller.snapshot().status, "conflict");
  });
}

test("PRD-146 closing advanced tools aborts its private request lane", async () => {
  const pending = deferred();
  let requestSignal;
  const controller = createWorkspaceNavigationController({
    createIdempotencyKey: (namespace) => `${namespace}:stable-key`,
    api: completeApi({
      advancedTools: ({ signal }) => {
        requestSignal = signal;
        return pending.promise;
      },
    }),
  });
  await controller.loadNavigation();
  const loading = controller.loadAdvancedTools().then(
    (value) => ({ value }),
    (error) => ({ error }),
  );
  await new Promise((resolve) => setImmediate(resolve));
  controller.cancelAdvancedTools();
  assert.equal(requestSignal.aborted, true);
  assert.equal(controller.snapshot().busy, false);
  pending.resolve(normalizeWorkspaceAdvancedTools(advancedToolsPayload()));
  assert.equal((await loading).error.status, 409);
});

test("PRD-146 390px shell starts with its navigation drawer closed", () => {
  const originalWindow = globalThis.window;
  globalThis.window = { innerWidth: 390 };
  try {
    const shell = renderConsoleShell({
      authContext: { user: { role: "owner" } },
      consoleShellState: {
        navigation: projectWorkspaceNavigationForConsole(
          normalizeWorkspaceNavigation(navigationPayload()),
          "workspace.home.overview",
        ),
        preferences: { preference: { sidebar_collapsed: false } },
      },
      contentHtml: "",
      i18n: { t: (_key, _params, fallback) => fallback },
    });
    assert.match(shell, /class="console-shell sidebar-collapsed"/u);
    assert.match(shell, /data-sidebar-collapsed="true"/u);
    assert.match(shell, /data-sidebar-toggle[\s\S]*aria-expanded="false"/u);
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});

test("PRD-146 renderers expose eight groups, one accordion, tasks, advanced dialog, and account scope", () => {
  const navigation = normalizeWorkspaceNavigation(navigationPayload());
  const projected = projectWorkspaceNavigationForConsole(
    navigation,
    "workspace.home.overview",
  );
  const shell = renderConsoleShell({
    authContext: {
      user: {
        display_name: "Workspace Owner",
        email: "owner@example.test",
        role: "owner",
        tenant_id: "hidden",
      },
      tenant: { name: "Acme" },
      session: { expires_at: "2026-09-03T09:00:00Z" },
    },
    consoleShellState: {
      navigation: projected,
      preferences: { preference: navigation.preference },
      activeNavigationItemId: "overview",
      activeRouteCode: "workspace.home.overview",
      sidebarExpandedGroupKey: "gateway",
    },
    contentHtml: renderWorkspaceTaskHome(
      normalizeWorkspaceGettingStarted(taskHomePayload()),
    ),
    i18n: { t: (_key, _params, fallback) => fallback },
  });
  const dialog = renderWorkspaceAdvancedTools(
    normalizeWorkspaceAdvancedTools(advancedToolsPayload()),
  );

  assert.equal((shell.match(/data-sidebar-group="/gu) ?? []).length, 8);
  assert.equal((shell.match(/class="sidebar-group is-collapsed"/gu) ?? []).length, 7);
  assert.match(shell, /data-sidebar-group="gateway"[^>]*>/u);
  assert.match(shell, /data-workspace-task-route="workspace.gateway.providers"/u);
  assert.match(shell, /class="user-menu-preferences" href="identity.html#account-preferences"/u);
  assert.match(shell, /data-workspace-advanced-open/u);
  assert.doesNotMatch(shell, /permission_fingerprint|owner_ref|contract_snapshot_id/u);
  assert.match(dialog, /<dialog class="workspace-advanced-dialog"/u);
  assert.match(dialog, /data-workspace-advanced-tool="provider_error_overrides"/u);
  assert.match(dialog, /data-workspace-advanced-tool="deployment_summary"[^>]*disabled/u);
  assert.doesNotMatch(dialog, /<dialog[^>]* open/u);
});

test("role-aware sidebar renders every authorized item without deferred controls", () => {
  const navigation = normalizeWorkspaceNavigation(navigationPayload());
  const profiles = {
    admin: ["home", "workspace", "gateway", "finance", "governance", "system"],
    developer: ["gateway", "finance", "developer-center"],
    finance_admin: ["gateway", "finance", "system"],
    viewer: ["home", "gateway", "finance"],
  };
  const activeRoutes = {
    admin: "workspace.home.overview",
    developer: "workspace.developer.sdk",
    finance_admin: "workspace.finance.subscription",
    viewer: "workspace.home.getting-started",
  };
  const activeIds = {
    admin: "overview",
    developer: "sdk-examples",
    finance_admin: "workspace-subscription",
    viewer: "quickstart",
  };
  for (const [role, expectedGroups] of Object.entries(profiles)) {
    const projected = projectWorkspaceNavigationForConsole(
      navigation,
      activeRoutes[role],
      null,
      { permission_codes: ["virtual_key.read"] },
      role,
    );
    const visibleGroups = [...new Set(projected.navigation_items
      .filter((item) => item.sidebar_visible)
      .map((item) => item.group_code))];
    assert.deepEqual(visibleGroups, expectedGroups, role);
    if (role === "developer") {
      assert.ok(projected.navigation_items.some((item) => item.id === "sdk-examples"), role);
      assert.ok(projected.navigation_items.some((item) => item.id === "usage"), role);
      assert.doesNotMatch(JSON.stringify(projected.navigation_items), /providers|local-models|private-gateways|models|model-aliases|routing-policies/u, role);
    } else if (role !== "viewer") {
      assert.ok(projected.navigation_items.some((item) => item.id === "providers"), role);
    }
    if (!["developer", "viewer"].includes(role)) {
      assert.ok(projected.navigation_items.some((item) => item.sidebar_visible === false), role);
    }
    const shell = renderConsoleShell({
      consoleShellState: {
        navigation: projected,
        preferences: { preference: navigation.preference },
        activeNavigationItemId: activeIds[role],
        activeRouteCode: activeRoutes[role],
        sidebarExpandedGroupKey: "home",
      },
      contentHtml: "",
      i18n: { t: (_key, _params, fallback) => fallback },
    });
    assert.doesNotMatch(shell, /sidebar-deferred-controls|sidebar-group-more|更多控制/u, role);
    for (const item of projected.navigation_items.filter((entry) => entry.sidebar_visible !== false)) {
      assert.match(shell, new RegExp(`data-console-nav="${item.id}"`, "u"), `${role}:${item.id}`);
    }
    assert.doesNotMatch(shell, /data-sidebar-default="false"/u, role);
    assert.match(shell, /data-sidebar-default="true"/u, role);
    if (role === "admin") {
      assert.equal((shell.match(/data-sidebar-group="/gu) ?? []).length, 6);
      assert.match(shell, /data-sidebar-group="home"/u);
    }
  }
});

test("employee lands on task home while first-use administrator lands on organization overview", () => {
  const navigation = normalizeWorkspaceNavigation({
    ...navigationPayload(),
    preference: { ...navigationPayload().preference, revision: 1, last_primary_route_code: "workspace.home.overview" },
  });
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "", { roleCode: "viewer" }).routeCode,
    "workspace.home.getting-started",
  );
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "", { roleCode: "admin" }).routeCode,
    "workspace.home.overview",
  );
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "workspace.home.getting-started", { roleCode: "viewer" }).routeCode,
    "workspace.home.getting-started",
  );
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "workspace.gateway.providers", { roleCode: "viewer" }).rejected,
    true,
  );
});

test("organization login without a hash always lands on the organization home", () => {
  const navigation = normalizeWorkspaceNavigation({
    ...navigationPayload(),
    preference: {
      ...navigationPayload().preference,
      revision: 9,
      last_primary_route_code: "workspace.gateway.models",
    },
  });
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "", { roleCode: "admin" }).routeCode,
    "workspace.home.overview",
  );
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "workspace.gateway.providers", { roleCode: "admin" }).routeCode,
    "workspace.gateway.providers",
  );
});

test("completed setup hash never becomes the developer or organization home", () => {
  const navigation = normalizeWorkspaceNavigation({
    ...navigationPayload(),
    developer_route_codes: [
      "workspace.developer.sdk",
      "workspace.developer.api",
    ],
    preference: { ...navigationPayload().preference, revision: 4, last_primary_route_code: "workspace.home.getting-started" },
  });
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "workspace.home.getting-started", { roleCode: "admin" }).routeCode,
    "workspace.home.overview",
  );
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "workspace.home.getting-started", { roleCode: "developer" }).routeCode,
    "workspace.developer.sdk",
  );
  assert.equal(
    resolveWorkspaceNavigationRoute(navigation, "workspace.gateway.providers", { roleCode: "developer" }).rejected,
    true,
  );
});

test("PRD-146 main fences route, logout, Workspace invalidation, and pagehide lifecycles", async () => {
  const [main, client, store, controller] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/workspaceNavigation.js", import.meta.url), "utf8"),
    readFile(
      new URL("../../src/features/workspace-navigation/workspaceNavigationStore.js", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL(
        "../../src/features/workspace-navigation/workspaceNavigationController.js",
        import.meta.url,
      ),
      "utf8",
    ),
  ]);
  const switchStart = main.indexOf("async function switchConsoleNavigation");
  const switchEnd = main.indexOf("function cancelPendingConsoleNavigationSideEffects", switchStart);
  const routeSwitch = main.slice(switchStart, switchEnd);
  assert.ok(routeSwitch.indexOf("workspaceNavigationController.changeRoute();") >= 0);
  assert.ok(
    routeSwitch.indexOf("workspaceNavigationController.changeRoute();")
      < routeSwitch.indexOf("loadAndRenderActiveNavigation"),
  );

  const pagehideStart = main.indexOf(
    'window.addEventListener("pagehide", () => {',
    main.indexOf("bindNavigationHashRefresh"),
  );
  const pagehideEnd = main.indexOf("void start();", pagehideStart);
  const pagehide = main.slice(pagehideStart, pagehideEnd);
  assert.match(pagehide, /workspaceNavigationController\.leave\(\)/u);
  assert.match(pagehide, /closeWorkspaceAdvancedToolsDialog\(\{ restoreFocus: false \}\)/u);
  assert.match(main, /workspaceNavigationController\.cancelAdvancedTools\(\)/u);
  assert.match(
    main,
    /function renderLogin[\s\S]*workspaceNavigationController\.leave\(\)/u,
  );
  assert.match(
    main,
    /function handleConsoleWorkspaceContextInvalidation[\s\S]*workspaceNavigationController\.leave\(\)/u,
  );
  assert.equal((main.match(/workspaceNavigationController\.loadGettingStarted\(\)/gu) ?? []).length, 2);
  assert.match(main, /async function loadWorkspaceConnectionProgress\(\)[\s\S]*?workspaceNavigationController\.loadGettingStarted\(\)/u);
  assert.doesNotMatch([client, store, controller].join("\n"), /localStorage|sessionStorage/u);
});

function completeApi(overrides = {}) {
  return {
    identitySession: async () => identitySession(),
    workspaceSession: async () => workspaceSession(),
    navigation: async () => normalizeWorkspaceNavigation(navigationPayload()),
    gettingStarted: async () => normalizeWorkspaceGettingStarted(taskHomePayload()),
    advancedTools: async () => normalizeWorkspaceAdvancedTools(advancedToolsPayload()),
    updatePreference: async () => normalizeWorkspaceNavigationPreference(preferencePayload()),
    openAdvancedTool: async () => normalizeWorkspaceAdvancedToolOpen(advancedOpenPayload()),
    ...overrides,
  };
}

function workspaceSession(workspaceId = WORKSPACE_ONE, role = "owner") {
  return {
    context: {
      workspace_id: workspaceId,
      role,
      session_status: "active",
      expires_at: "2026-09-08T00:00:00Z",
    },
    authorization: { permission_codes: ["console.basic.read"], permission_revision: 10,
      permission_fingerprint: FINGERPRINT, membership_revision: 9 },
  };
}

function identitySession(sessionId = "identity-session-one") {
  return { account: { status: "active" }, current_session: {
    session_id: sessionId, revoke_generation: 1, is_current: true, expires_at: "2026-09-09T00:00:00Z",
  } };
}

function navigationPayload() {
  return {
    account_action_codes: ["identity.preferences"],
    account_locale: "en-US",
    advanced_tools_available: true,
    contract_generation: 4,
    contract_snapshot_id: SNAPSHOT,
    contract_version: "workspace-navigation-v1",
    page_visit_session_ref: "b".repeat(64),
    detail_route_codes: [
      "workspace.deployment.branding",
      "workspace.gateway.routing-policies",
      "workspace.finance.provider-costs",
      "workspace.applications.file-security",
      "workspace.observability.request-explanation",
    ],
    developer_route_codes: [
      "workspace.developer.sdk",
      "workspace.developer.migration",
      "workspace.developer.api",
      "workspace.developer.mcp",
    ],
    groups: [
      group("home", [
        item("workspace.home.overview", "primary", 10),
        item("workspace.home.getting-started", "task_home", 20),
      ]),
      group("workspace", [item("workspace.settings.profile", "primary", 30)]),
      group("gateway", [
        item("workspace.gateway.providers", "primary", 40),
        item("workspace.gateway.virtual-keys", "primary", 41),
      ]),
      group("finance", [
        item("workspace.finance.subscription", "primary", 50),
        item("workspace.finance.usage", "primary", 51),
      ]),
      group("governance", [
        item("workspace.governance.policies", "primary", 60),
        item("workspace.observability.requests", "primary", 61),
      ]),
      group("applications", [item("workspace.applications.chat", "primary", 70)]),
      group("developer-center", []),
      group("system", [item("workspace.observability.feedback", "primary", 80)]),
    ],
    item_set_digest: "d".repeat(64),
    membership_revision: 9,
    navigation_epoch: 8,
    observed_at: "2026-09-03T08:00:00Z",
    owner_ref: "a".repeat(64),
    permission_fingerprint: FINGERPRINT,
    permission_revision: 10,
    preference: {
      revision: 2,
      last_primary_route_code: "workspace.home.overview",
      expanded_group_code: null,
      sidebar_collapsed: false,
    },
    projection_generation: 6,
    projection_status: "ready",
    request_id: "req_navigation",
    trace_id: "trc_navigation",
  };
}

function group(groupCode, items) {
  return {
    group_code: groupCode,
    label_key: `navigation.group.${groupCode}`,
    expanded: false,
    items,
  };
}

function item(routeCode, kind, order) {
  return {
    route_code: routeCode,
    label_key: `navigation.${order}.label`,
    description_key: `navigation.${order}.description`,
    kind,
    order,
  };
}

test("completed task actions stay available without becoming the next task", () => {
  const value = taskHomePayload();
  value.completed_count = 1;
  value.tasks.unshift({ ...value.tasks[0], task_code: "model_catalog_ready", status: "completed",
    action_route_code: "workspace.gateway.models" });
  assert.equal(normalizeWorkspaceGettingStarted(value).next_task_code, "provider_connected");
  assert.equal(normalizeWorkspaceGettingStarted(value).tasks[0].action_enabled, true);
  value.next_task_code = "model_catalog_ready";
  assert.throws(() => normalizeWorkspaceGettingStarted(value));
  value.tasks = [value.tasks[0]];
  value.next_task_code = null;
  assert.equal(normalizeWorkspaceGettingStarted(value).next_task_code, null);
  value.tasks[0].status = "blocked";
  value.completed_count = 0;
  assert.throws(() => normalizeWorkspaceGettingStarted(value));
});

function taskHomePayload() {
  return {
    completed_count: 0,
    contract_generation: 4,
    contract_snapshot_id: SNAPSHOT,
    evidence_digest: "e".repeat(64),
    membership_revision: 9,
    navigation_epoch: 8,
    needs_attention_count: 0,
    next_task_code: "provider_connected",
    observed_at: "2026-09-03T08:00:00Z",
    owner_ref: "a".repeat(64),
    permission_fingerprint: FINGERPRINT,
    permission_revision: 10,
    request_id: "req_tasks",
    role_code: "owner",
    source_freshness: "fresh",
    task_generation: 3,
    tasks: [
      {
        action_enabled: true,
        action_route_code: "workspace.gateway.providers",
        description_key: "tasks.provider.description",
        priority: 10,
        source_freshness: "fresh",
        status: "ready",
        task_code: "provider_connected",
        title_key: "tasks.provider.title",
      },
    ],
    trace_id: "trc_tasks",
  };
}

function advancedToolsPayload() {
  return {
    contract_generation: 4,
    contract_snapshot_id: SNAPSHOT,
    entitlement_generation: 5,
    membership_revision: 9,
    navigation_epoch: 8,
    observed_at: "2026-09-03T08:00:00Z",
    owner_ref: "a".repeat(64),
    permission_fingerprint: FINGERPRINT,
    permission_revision: 10,
    request_id: "req_advanced",
    tools: [
      {
        action_enabled: true,
        availability: "available",
        entitlement_revision: 7,
        label_key: "advanced.provider-errors.label",
        observed_at: "2026-09-03T08:00:00Z",
        reason_code: null,
        route_code: "workspace.advanced.provider-errors",
        tool_code: "provider_error_overrides",
      },
      {
        action_enabled: false,
        availability: "blocked",
        entitlement_revision: 4,
        label_key: "advanced.deployment-summary.label",
        observed_at: "2026-09-03T08:00:00Z",
        reason_code: "deployment_not_ready",
        route_code: "workspace.advanced.deployment-summary",
        tool_code: "deployment_summary",
      },
    ],
    trace_id: "trc_advanced",
  };
}

function preferencePayload() {
  return {
    changed: true,
    contract_generation: 4,
    contract_snapshot_id: SNAPSHOT,
    expanded_group_code: "gateway",
    idempotent_replay: false,
    last_primary_route_code: "workspace.home.overview",
    membership_revision: 9,
    navigation_epoch: 8,
    permission_fingerprint: FINGERPRINT,
    permission_revision: 10,
    preference_id: `nvp_${"4".repeat(32)}`,
    request_id: "req_preference",
    revision: 3,
    sidebar_collapsed: false,
    trace_id: "trc_preference",
    updated_at: "2026-09-03T08:00:00Z",
    workspace_ref: "b".repeat(64),
  };
}

function advancedOpenPayload() {
  return {
    contract_generation: 4,
    contract_snapshot_id: SNAPSHOT,
    entitlement_revision: 7,
    idempotent_replay: false,
    membership_revision: 9,
    navigation_epoch: 8,
    owner_ref: "a".repeat(64),
    permission_fingerprint: FINGERPRINT,
    permission_revision: 10,
    request_id: "req_open",
    route_code: "workspace.advanced.provider-errors",
    tool_code: "provider_error_overrides",
    trace_id: "trc_open",
  };
}

function privateResponse(payload, etag, overrides = {}) {
  const headers = new Map(Object.entries({
    "cache-control": "no-store, private",
    etag,
    pragma: "no-cache",
    vary: "Cookie",
    "x-permission-fingerprint": payload.permission_fingerprint,
    ...Object.fromEntries(
      Object.entries(overrides).map(([key, value]) => [key.toLowerCase(), value]),
    ),
  }));
  return {
    ok: true,
    status: 200,
    headers: {
      get(name) {
        return headers.get(String(name).toLowerCase()) ?? null;
      },
    },
    async json() {
      return structuredClone(payload);
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}
