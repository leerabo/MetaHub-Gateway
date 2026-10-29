import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS, hasPermission } from "../../src/lib/permissions.js";
import { WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../src/lib/workspaceNavigation.js";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";
import { workspaceConsoleSessionFromShell } from "../../src/lib/workspaceConsoleSession.js";
import { createWorkspaceResourcePage, workspaceResourcePageOwner } from "../../src/lib/workspaceResourcePage.js";
import { createRoutingPolicyPageMemory } from "../../src/features/routing-policies/renderRoutingPolicyPanel.js";
import { createCacheAffinityPageMemory } from "../../src/features/routing-policies/cacheAffinityControls.js";
import { createModelQualityScorePageMemory, qualityPercentToBps } from "../../src/features/model-quality-scores/renderModelQualityScoresPanel.js";
import { createExperimentPageState, experimentPageOwner } from "../../src/features/experiments/experimentPageState.js";
import { renderExperiments } from "../../src/features/experiments/renderExperiments.js";
import { bindExperimentInteractions } from "../../src/features/experiments/bindExperiments.js";

const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
function production(name) {
  const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  const end = start + source.slice(start).search(/\n}\r?\n/);
  assert.ok(start >= 0 && end > start, name);
  return source.slice(start, end + 2);
}
function shell(navigation, workspace = "1", permissions = Object.values(PERMISSIONS)) {
  const expires = "2099-01-01T00:00:00Z";
  const identity = { account: { status: "active" }, current_session: {
    session_id: "public-session", is_current: true, revoke_generation: 0, expires_at: expires,
  } };
  const context = { workspace_id: `wsp_${workspace.repeat(32)}`, role: "owner", session_status: "active", expires_at: expires };
  const authorization = { permission_codes: [...permissions], permission_revision: 1, membership_revision: 2, permission_fingerprint: "a".repeat(64) };
  const route = WORKSPACE_SOURCE_ROUTE_REGISTRY[navigation];
  return { state: "ok", workspaceNavigationContract: true, activeNavigationItemId: navigation, activeRouteCode: route,
    workspaceIdentity: identity, workspaceContext: context, workspaceAuthorization: authorization,
    workspaceOwner: workspaceNavigationOwner(identity, { context, authorization }),
    navigation: { permission_revision: 1, membership_revision: 2, permission_fingerprint: "a".repeat(64),
      active_navigation_item_id: navigation, active_route_code: route,
      navigation_items: [{ id: navigation, route_code: route, target_section: navigation, visible: true, disabled: false }] } };
}
class Element {
  isConnected = true;
  listeners = new Map();
  dataset = { modelAliasId: "alias_sample", modelId: "model_sample" };
  elements = {};
  addEventListener(type, handler) { this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler]); }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  fire(type) { return Promise.all((this.listeners.get(type) ?? []).map((handler) => handler({ preventDefault() {} }))); }
}
const alias = { model_alias_id: "alias_sample", tenant_id: "ten_fact", alias: "sample", status: "active",
  default_model_id: "model_sample", routing_policy: { routing_policy_id: "rpol_sample", model_alias_id: "alias_sample",
    routing_mode: "fixed", version: 1, status: "enabled", candidates: [], warnings: [] }, cache_affinity: null };
const score = { model_quality_score_id: "mqs_sample", model_id: "model_sample", tenant_id: "ten_fact",
  internal_model_id: "sample", status: "active", model_status: "active", feedback_version: 1,
  manual_quality_bps: 7000, statistics_version: 0 };
function harness(domain) {
  const navigation = domain === "score" ? "model-quality-scores" : "routing-policies";
  const form = new Element();
  const input = new Element();
  input.value = "85";
  form.elements.manualQualityPercent = input;
  form.elements.cacheAffinityEnabled = Object.assign(new Element(), { checked: true });
  form.elements.cacheAffinityDataClass = { value: "test" };
  form.elements.cacheAffinityMinimumPrefix = { value: "256" };
  form.elements.cacheAffinityTtl = Object.assign(new Element(), { value: "60" });
  let resolve, reject;
  const pending = new Promise((yes, no) => { resolve = yes; reject = no; });
  const calls = [], effects = [];
  const routeMemory = createRoutingPolicyPageMemory({ requestIdFactory: () => "req_route" });
  const cacheMemory = createCacheAffinityPageMemory({ requestIdFactory: () => "req_cache" });
  const scoreMemory = createModelQualityScorePageMemory({ requestIdFactory: () => "req_score" });
  const selector = domain === "score" ? "[data-model-quality-form]" : domain === "cache" ? "[data-cache-affinity-form]" : "[data-routing-policy-form]";
  const scope = { AuthApiError, PERMISSIONS, hasPermission, workspaceResourcePageOwner,
    authSession: null, resourcePageShellState: shell(navigation), refreshSequence: 1,
    hash: navigation, controlApiUrl: "https://control.test", modelQualityScoreFilters: { modelId: "", limit: 100, offset: 0 },
    routingPolicyPageMemory: routeMemory, cacheAffinityPageMemory: cacheMemory, modelQualityScorePageMemory: scoreMemory,
    readRoutingQualityCostConfig: () => ({}), readCacheAffinityDraft: () => ({ status: "enabled", data_class: "test", minimum_prefix_chars: 256, ttl_seconds: 60 }),
    modelQualityDraftFromElement: () => { effects.push("read-input"); return { valid: true, value: qualityPercentToBps(input.value) }; },
    renderRoutingPolicyPanelFromMemory: () => effects.push("render"),
    renderModelQualityScorePanelFromMemory: () => effects.push("render"),
    updateModelQualityFormState() {}, updateRoutingQualityCostFormState() {}, updateCacheAffinityFormState() {},
    handleResourceAuthorizationError: () => { effects.push("auth-error"); return true; },
    refresh: async () => effects.push("refresh"),
    document: { querySelector: (value) => value === `.${navigation}-panel` ? form : null,
      querySelectorAll: (value) => value === selector ? [form] : [] },
  };
  scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
  scope.readActiveNavigationHash = () => scope.hash;
  scope.resourcePageLifecycle = createWorkspaceResourcePage({ current: () => ({
    session: scope.authSession, shell: scope.resourcePageShellState, hash: scope.hash, refreshGeneration: scope.refreshSequence,
  }) });
  scope.resourcePageLifecycle.enter();
  scope.resourcePageLifecycle.render();
  for (const request of ["updateRoutingMode", "updateCacheAffinity", "updateModelQualityScore"]) {
    scope[request] = async (options) => { calls.push(options); return pending; };
  }
  const names = ["bindResourceElement", "currentEvaluationPageIdentity", "currentRoutingPolicyOwner", "currentModelQualityScoreOwner",
    "bindRoutingPolicyInteractions", "bindCacheAffinityInteractions", "bindModelQualityScoreInteractions"];
  Object.assign(scope, runInNewContext(`${names.map(production).join("\n")}\n({${names.join(",")}})`, scope));
  const identity = scope.currentEvaluationPageIdentity(navigation, domain === "score" ? PERMISSIONS.MODEL_QUALITY_READ : PERMISSIONS.ROUTING_READ);
  assert.ok(identity);
  routeMemory.acceptRefresh(routeMemory.beginRefresh({ identity, navigation }), [alias]);
  cacheMemory.acceptRefresh(cacheMemory.beginRefresh({ identity, navigation,
    workspaceOwner: workspaceResourcePageOwner(scope.authSession, scope.resourcePageShellState, { navigationId: navigation }),
  }), [alias]);
  scoreMemory.acceptRefresh(scoreMemory.beginRefresh({ identity, navigation, query: "" }), { model_quality_scores: [score], total: 1, limit: 100, offset: 0 });
  if (domain === "route") routeMemory.selectMode(alias.model_alias_id, "cost_first");
  if (domain === "score") scope.bindModelQualityScoreInteractions(scope.resourcePageShellState);
  else scope.bindRoutingPolicyInteractions(scope.resourcePageShellState);
  return { scope, form, calls, effects, resolve, reject,
    switchOwner() {
      scope.resourcePageShellState = shell(navigation, "2");
      scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
      scope.resourcePageLifecycle.enter();
      routeMemory.invalidate(); cacheMemory.invalidate(); scoreMemory.invalidate();
    },
  };
}

for (const domain of ["route", "cache", "score"]) {
  test(`PRD-147 actual ${domain} saves current Workspace response and clears busy state`, async () => {
    const h = harness(domain);
    const operation = h.form.fire("submit");
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].authentication, "workspace");
    const result = domain === "route"
      ? { changed: true, routing_policy: { ...alias.routing_policy, routing_mode: "cost_first", version: 2 } }
      : domain === "score"
        ? { changed: true, model_quality_score: { ...score, manual_quality_bps: 8500, feedback_version: 2 } }
        : { changed: true, request_id: h.calls[0].requestId, cache_affinity: {
          cache_affinity_id: `caf_${"a".repeat(32)}`, tenant_id: alias.tenant_id, model_alias_id: alias.model_alias_id,
          status: "enabled", data_class: "test", minimum_prefix_chars: 256, ttl_seconds: 60, version: 1,
          created_at: "2026-09-08T00:00:00Z", updated_at: "2026-09-08T00:00:00Z",
        } };
    h.resolve(result);
    await operation;
    const memory = domain === "route" ? h.scope.routingPolicyPageMemory
      : domain === "cache" ? h.scope.cacheAffinityPageMemory : h.scope.modelQualityScorePageMemory;
    const row = memory.snapshot().rows[0];
    assert.equal(row.busy, false);
    assert.equal(row.dirty, false);
    assert.equal(row.actionError, null);
    assert.ok(h.effects.includes("refresh"));
    assert.equal(h.effects.includes("auth-error"), false);
  });
  for (const boundary of ["workspace", "hash", "detached", "render"]) {
    test(`PRD-147 actual ${domain} handler refuses ${boundary} before reading inputs`, async () => {
      const h = harness(domain);
      if (boundary === "workspace") h.switchOwner();
      if (boundary === "hash") h.scope.hash = "projects";
      if (boundary === "detached") h.form.isConnected = false;
      if (boundary === "render") h.scope.resourcePageLifecycle.render();
      await h.form.fire("submit");
      assert.equal(h.calls.length, 0);
      assert.deepEqual(h.effects, []);
    });
  }
  for (const outcome of ["success", "401", "403", "409"]) {
    test(`PRD-147 actual ${domain} ignores late ${outcome} after Workspace switch`, async () => {
      const h = harness(domain);
      const operation = h.form.fire("submit");
      await h.form.fire("submit");
      assert.equal(h.calls.length, 1);
      assert.equal(h.calls[0].authentication, "workspace");
      assert.equal(Object.hasOwn(h.calls[0], "token"), false);
      h.switchOwner();
      const effects = [...h.effects];
      if (outcome === "success") h.resolve({});
      else h.reject(new AuthApiError({ status: Number(outcome), code: "auth.permission_denied" }));
      await operation;
      assert.deepEqual(h.effects, effects);
      assert.equal(h.calls[0].signal.aborted, true);
    });
  }
}

test("PRD-147 cache Workspace refresh rejects absent owner, mixed tenants and tenant replacement", () => {
  const currentShell = shell("routing-policies");
  const session = workspaceConsoleSessionFromShell(currentShell);
  const workspaceOwner = workspaceResourcePageOwner(session, currentShell, { navigationId: "routing-policies" });
  const memory = createCacheAffinityPageMemory();
  const options = { identity: workspaceOwner.key, navigation: "routing-policies" };
  memory.acceptRefresh(memory.beginRefresh(options), [alias]);
  assert.equal(memory.snapshot().rows.length, 0);
  const ticket = memory.beginRefresh({ ...options, workspaceOwner });
  assert.equal(memory.acceptRefresh(ticket, [alias, { ...alias, model_alias_id: "other", tenant_id: "other" }]), false);
  assert.equal(memory.snapshot().rows.length, 0);
  assert.equal(memory.acceptRefresh(ticket, [alias]), true);
  assert.equal(memory.snapshot().rows.length, 1);
  memory.acceptRefresh(ticket, [{ ...alias, tenant_id: "other" }]);
  assert.equal(memory.snapshot().rows.some((row) => row.tenantId === "other"), false);
});

test("PRD-147 experiment Workspace owner uses public session fields without a Legacy projection", () => {
  const a = shell("experiments");
  const session = workspaceConsoleSessionFromShell(a);
  const owner = experimentPageOwner(session.context, a, { session });
  assert.ok(owner?.canCreate);
  assert.equal(owner.workspaceId, a.workspaceContext.workspace_id);
  assert.equal(Object.hasOwn(owner, "tenantId"), false);
  assert.ok(renderExperiments(null, session.context, a));
  assert.equal(experimentPageOwner(session.context, shell("experiments", "2"), { session }), null);
  const memory = createExperimentPageState();
  memory.enter(owner);
  const ticket = memory.beginList(owner, 1);
  const b = shell("experiments", "2");
  const next = workspaceConsoleSessionFromShell(b);
  memory.enter(experimentPageOwner(next.context, b, { session: next }));
  assert.equal(ticket.controller.signal.aborted, true);
  assert.equal(memory.isListCurrent(ticket, owner), false);
  for (const permission of [PERMISSIONS.EXPERIMENT_WRITE, PERMISSIONS.PROJECT_READ, PERMISSIONS.ENVIRONMENT_READ, PERMISSIONS.ROUTING_READ, PERMISSIONS.MODEL_READ]) {
    const reduced = shell("experiments", "1", Object.values(PERMISSIONS).filter((code) => code !== permission));
    const current = workspaceConsoleSessionFromShell(reduced);
    assert.equal(experimentPageOwner(current.context, reduced, { session: current }).canCreate, false);
    assert.equal(experimentPageOwner(current.context, reduced, { session: current, mode: "create" }), null);
  }
});

test("PRD-147 experiment old controls refuse events before reading selection", async () => {
  const workspace = new Element();
  const status = new Element();
  let reads = 0;
  Object.defineProperty(status, "value", { get() { reads += 1; return "active"; } });
  workspace.querySelector = (selector) => selector === "[data-experiment-status]" ? status : null;
  let calls = 0;
  bindExperimentInteractions({ isCurrent: () => false, onFilter: () => { calls += 1; } }, { querySelector: () => workspace });
  await status.fire("change");
  assert.equal(reads, 0);
  assert.equal(calls, 0);
});

function experimentHarness(action) {
  const currentShell = shell("experiments");
  const session = workspaceConsoleSessionFromShell(currentShell);
  const state = createExperimentPageState();
  const item = { experiment_id: `exp_${"a".repeat(32)}`, model_a: {}, model_b: {} };
  const owner = experimentPageOwner(session.context, currentShell, { session });
  state.enter(owner);
  const initial = state.beginList(owner, 1);
  state.acceptList(initial, { experiments: [item], next_cursor: null }, owner, 1, {
    projects: [{ project_id: "project_test", status: "active" }],
    environments: [{ environment_id: "environment_test", status: "active" }],
    modelAliases: [{ model_alias_id: "alias_test", status: "active" }],
    models: ["a", "b"].map((id) => ({ model_id: `model_${id}`, status: "active", capabilities: { chat: true } })),
  });
  state.finishList(initial);
  if (action === "create") assert.equal(state.openCreate(owner), true);
  if (action === "detail") assert.equal(state.selectExperiment(item.experiment_id), true);
  const calls = [], effects = [];
  let resolve, reject;
  const pending = new Promise((yes, no) => { resolve = yes; reject = no; });
  const scope = { AuthApiError, PERMISSIONS, hasPermission, workspaceResourcePageOwner, experimentPageOwner,
    authSession: session, resourcePageShellState: currentShell, hash: "experiments", refreshSequence: 1,
    experimentPageState: state, controlApiUrl: "https://control.test", readActiveNavigationHash: () => scope.hash,
    refresh: async () => effects.push("refresh"),
    isExperimentAbortError: (error) => error?.name === "AbortError",
    handleExperimentAuthenticationFailure: () => { effects.push("auth-error"); return true; },
    isExperimentPermissionRevocation: () => false,
  };
  for (const name of ["createExperiment", "listExperiments", "getExperiment", "getExperimentResults"]) {
    scope[name] = async (options) => { calls.push(options); return pending; };
  }
  const names = ["currentEvaluationPageIdentity", "currentExperimentOwner", "submitExperiment", "loadExperimentsState", "loadExperimentDetailState"];
  Object.assign(scope, runInNewContext(`${names.map(production).join("\n")}\n({${names.join(",")}})`, scope));
  return { state, scope, calls, effects, resolve, reject, item,
    run: () => action === "create" ? scope.submitExperiment({}, "operation-key", currentShell)
      : action === "list" ? scope.loadExperimentsState(currentShell, 1, {})
        : scope.loadExperimentDetailState(currentShell, 1),
    switchOwner() {
      scope.resourcePageShellState = shell("experiments", "2");
      scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
      state.enter(experimentPageOwner(scope.authSession.context, scope.resourcePageShellState, { session: scope.authSession }));
    },
  };
}

for (const action of ["list", "detail", "create"]) {
  for (const outcome of ["success", "401", "403", "409"]) {
    test(`PRD-147 actual experiment ${action} ignores late ${outcome} and finally after Workspace switch`, async () => {
      const h = experimentHarness(action);
      const pending = h.run();
      assert.equal(h.calls.length, action === "detail" ? 2 : 1);
      assert.ok(h.calls.every((call) => call.authentication === "workspace" && !Object.hasOwn(call, "token")));
      h.switchOwner();
      const before = h.state.snapshot();
      if (outcome === "success") h.resolve({ experiment: h.item, experiments: [h.item], next_cursor: null });
      else h.reject(new AuthApiError({ status: Number(outcome), code: "auth.permission_denied" }));
      await pending;
      assert.deepEqual(h.state.snapshot(), before);
      assert.deepEqual(h.effects, []);
      assert.ok(h.calls.every((call) => call.signal.aborted));
    });
  }
}

for (const action of ["list", "create"]) {
  test(`PRD-147 actual experiment ${action} accepts current response`, async () => {
    const h = experimentHarness(action);
    const pending = h.run();
    h.resolve({ experiment: h.item, experiments: [h.item], next_cursor: null, request_id: "operation-result" });
    await pending;
    assert.equal(h.state.snapshot()[action === "list" ? "state" : "createState"], action === "list" ? "ready" : "success");
    assert.equal(h.effects.includes("auth-error"), false);
  });
}
