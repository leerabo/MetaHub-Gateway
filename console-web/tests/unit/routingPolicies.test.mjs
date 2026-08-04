// 本单测覆盖 PAGE-067 客户端、行级代次、迟到响应、RBAC、XSS 和响应式边界。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  isConsoleNavigationOwnerCurrent,
  renderDeveloperStart,
  renderRoutingPoliciesLoading,
} from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createRoutingPolicyPageMemory,
  isRoutingQualityCostConfigValid,
  renderRoutingPolicyPanel,
  routingMinimumSampleCount,
  routingPolicyIdentitySnapshot,
  routingQualityPercentToBps,
} from "../../src/features/routing-policies/renderRoutingPolicyPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createRoutingPolicyRequestId,
  listModelAliases,
  updateRoutingMode,
} from "../../src/lib/modelAliases.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const adminContext = withPermissionSnapshot({
  user: {
    tenant_id: "tenant_1",
    user_id: "user_admin",
    email: "admin@example.com",
    display_name: "Admin",
    role: "admin",
  },
  tenant: { tenant_id: "tenant_1", name: "Tenant" },
  session: { session_id: "session_1", session_revision: 3, expires_at: "2026-07-19T00:00:00Z" },
}, [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE]);

const qualityCostConfig = Object.freeze({
  quality_weight_bps: 5000,
  cost_weight_bps: 3000,
  latency_weight_bps: 2000,
  quality_floor_bps: 6000,
  minimum_sample_count: 20,
});

/** 构造只包含安全主备摘要的 ModelAlias 响应。 */
function aliasFixture({ version = 1, mode = "fixed", alias = "company-default", config = qualityCostConfig } = {}) {
  return {
    model_alias_id: "alias_1",
    tenant_id: "tenant_1",
    alias,
    environment: "prod",
    environment_name: "生产",
    default_model_id: "model_1",
    default_internal_model_id: "deepseek-chat",
    default_provider_code: "deepseek",
    fallback_model_id: "model_2",
    fallback_internal_model_id: "qwen-plus",
    fallback_provider_code: "qwen",
    status: "active",
    routing_policy: {
      routing_policy_id: "rpol_1",
      model_alias_id: "alias_1",
      routing_mode: mode,
      quality_cost_config: { ...config },
      status: "enabled",
      version,
      candidates: [
        { role: "default", model_id: "model_1", internal_model_id: "deepseek-chat", provider_code: "deepseek" },
        { role: "fallback", model_id: "model_2", internal_model_id: "qwen-plus", provider_code: "qwen" },
      ],
      warnings: [],
      created_at: "2026-07-18T09:00:00Z",
      updated_at: `2026-07-18T09:0${version}:00Z`,
    },
    effective_routing_mode: mode,
    routing_policy_implicit: false,
    updated_at: `2026-07-18T09:0${version}:00Z`,
  };
}

/** 建立完成首轮 GET 的页面仓库，便于测试保存与刷新交错。 */
function initializedMemory() {
  let requestSequence = 0;
  const memory = createRoutingPolicyPageMemory({
    requestIdFactory: () => `req_route_${++requestSequence}`,
  });
  const identity = routingPolicyIdentitySnapshot(adminContext);
  const refresh = memory.beginRefresh({ identity, navigation: "routing-policies" });
  assert.equal(memory.acceptRefresh(refresh, [aliasFixture()]), true);
  return { memory, identity };
}

test("routing mode client sends exact CAS contract without tenant and preserves request id", async () => {
  const calls = [];
  const controller = new AbortController();
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_header"]]),
      async json() {
        return {
          routing_policy: aliasFixture({ version: 2, mode: "cost_first" }).routing_policy,
          changed: true,
          request_id: "req_route_exact",
          trace_id: "trc_route_exact",
        };
      },
    };
  };

  const result = await updateRoutingMode({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    modelAliasId: "alias/1",
    routingMode: "cost_first",
    expectedVersion: 1,
    requestId: "req_route_exact",
    signal: controller.signal,
  });

  assert.equal(result.routing_policy.version, 2);
  assert.equal(calls[0].url, "http://control.local/model-aliases/alias%2F1/routing-mode");
  assert.equal(calls[0].options.method, "PATCH");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_route_exact");
  assert.equal(calls[0].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    routing_mode: "cost_first",
    expected_version: 1,
  });
  assert.doesNotMatch(calls[0].options.body, /tenant|candidate|condition|action/);
});

test("quality_cost client sends complete five-field config while stripping unknown fields", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map(),
      async json() {
        return {
          routing_policy: aliasFixture({ version: 2, mode: "quality_cost" }).routing_policy,
          changed: true,
          request_id: "req_quality_route",
        };
      },
    };
  };

  await updateRoutingMode({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    modelAliasId: "alias_1",
    routingMode: "quality_cost",
    expectedVersion: 1,
    qualityCostConfig: {
      ...qualityCostConfig,
      tenant_id: "must-not-send",
      statistics_version: 99,
    },
    requestId: "req_quality_route",
  });
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    routing_mode: "quality_cost",
    expected_version: 1,
    quality_cost_config: qualityCostConfig,
  });
  assert.doesNotMatch(calls[0].options.body, /tenant|statistics|actor/);
});

test("quality_cost percentage parsing and validation use exact integer bps", () => {
  assert.equal(routingQualityPercentToBps("50"), 5000);
  assert.equal(routingQualityPercentToBps("30.01"), 3001);
  assert.equal(routingQualityPercentToBps("0.001"), null);
  assert.equal(routingQualityPercentToBps("1e2"), null);
  assert.equal(routingMinimumSampleCount("20"), 20);
  assert.equal(routingMinimumSampleCount("20.0"), null);
  assert.equal(routingMinimumSampleCount("10001"), null);
  assert.equal(isRoutingQualityCostConfigValid(qualityCostConfig), true);
  assert.equal(isRoutingQualityCostConfigValid({ ...qualityCostConfig, latency_weight_bps: 1999 }), false);
  assert.equal(isRoutingQualityCostConfigValid({ ...qualityCostConfig, quality_weight_bps: 0, cost_weight_bps: 8000 }), false);
});

test("routing mode client maps 403 409 422 and non JSON errors with request id", async () => {
  for (const scenario of [
    { status: 403, code: "auth.permission_denied", requestId: "req_403" },
    { status: 409, code: "model_alias.routing_policy_version_conflict", requestId: "req_409" },
    { status: 422, code: "model_alias.routing_mode_invalid", requestId: "req_422" },
  ]) {
    globalThis.fetch = async () => ({
      ok: false,
      status: scenario.status,
      headers: new Map([["X-Request-ID", "req_header"]]),
      async json() {
        return {
          code: scenario.code,
          message: "safe error",
          request_id: scenario.requestId,
          current_routing_policy: scenario.status === 409
            ? {
                ...aliasFixture({ version: 2 }).routing_policy,
                candidates: [{
                  candidate_role: "default",
                  configured_priority: 0,
                  model_id: "model_1",
                  internal_model_id: "deepseek-chat",
                  provider_code: "deepseek",
                  provider_key: "must-not-enter-error-state",
                }],
              }
            : undefined,
        };
      },
    });
    await assert.rejects(
      () => updateRoutingMode({
        controlApiUrl: "http://control.local",
        token: "jwt-token",
        modelAliasId: "alias_1",
        routingMode: "cost_first",
        expectedVersion: 1,
      }),
      (error) => {
        assert.ok(error instanceof AuthApiError);
        assert.equal(error.status, scenario.status);
        assert.equal(error.code, scenario.code);
        assert.equal(error.requestId, scenario.requestId);
        // 只有冲突响应允许附带当前安全 Policy 摘要，其他错误不得伪造权威版本。
        if (scenario.status === 409) {
          assert.equal(error.currentRoutingPolicy.version, 2);
          assert.doesNotMatch(JSON.stringify(error.currentRoutingPolicy), /provider_key|must-not-enter-error-state/);
        }
        return true;
      },
    );
  }

  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    headers: new Map([["X-Request-ID", "req_non_json"]]),
    async json() {
      // 非 JSON 代理响应必须走 header request_id 兜底，不能把正文带入异常。
      throw new SyntaxError("not json");
    },
  });
  await assert.rejects(
    () => updateRoutingMode({
      controlApiUrl: "http://control.local",
      token: "jwt-token",
      modelAliasId: "alias_1",
      routingMode: "cost_first",
      expectedVersion: 1,
    }),
    (error) => error.requestId === "req_non_json" && error.status === 503,
  );
});

test("generic model alias requests keep unique ids for same-millisecond concurrency", async () => {
  const requestIds = [];
  const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  const originalNow = Date.now;
  const originalRandom = Math.random;
  try {
    // 强制进入无 Web Crypto 的极旧浏览器兜底，并让 Math.random 抛错，锁定实现不得使用弱随机源。
    Object.defineProperty(globalThis, "crypto", { value: undefined, configurable: true });
    Date.now = () => 1_721_280_000_000;
    Math.random = () => {
      // 一旦实现退回弱随机源就立即失败，避免仅靠结果不碰撞掩盖安全回归。
      throw new Error("request_id must not use Math.random");
    };
    globalThis.fetch = async (_url, options) => {
      requestIds.push(options.headers["X-Request-ID"]);
      return {
        ok: true,
        headers: new Map(),
        async json() {
          return { model_aliases: [], request_id: options.headers["X-Request-ID"] };
        },
      };
    };
    await Promise.all([
      listModelAliases({ controlApiUrl: "http://control.local", token: "token" }),
      listModelAliases({ controlApiUrl: "http://control.local", token: "token" }),
    ]);
    const routingIds = [createRoutingPolicyRequestId(), createRoutingPolicyRequestId()];
    assert.notEqual(routingIds[0], routingIds[1]);
  } finally {
    Date.now = originalNow;
    Math.random = originalRandom;
    if (cryptoDescriptor) {
      Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
    } else {
      delete globalThis.crypto;
    }
  }
  assert.equal(requestIds.length, 2);
  assert.notEqual(requestIds[0], requestIds[1]);
  assert.match(requestIds[0], /^console_model_aliases_/);
});

test("routing request ids use getRandomValues when randomUUID is unavailable", () => {
  const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  let randomCalls = 0;
  try {
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: {
        getRandomValues(bytes) {
          randomCalls += 1;
          bytes.fill(7);
          return bytes;
        },
      },
    });
    const first = createRoutingPolicyRequestId();
    const second = createRoutingPolicyRequestId();
    assert.match(first, /^req_console_routing_policy_07070707070707070707070707070707_/);
    assert.notEqual(first, second);
  } finally {
    if (cryptoDescriptor) {
      Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
    } else {
      delete globalThis.crypto;
    }
  }
  assert.equal(randomCalls, 2);
});

test("busy row accepts a valid PATCH 200 after one or more background refreshes", () => {
  const { memory, identity } = initializedMemory();
  assert.equal(memory.selectMode("alias_1", "cost_first"), true);
  const acquired = memory.acquireSave({ identity, navigation: "routing-policies", modelAliasId: "alias_1" });
  const duplicate = memory.acquireSave({ identity, navigation: "routing-policies", modelAliasId: "alias_1" });
  assert.equal(acquired.reused, false);
  assert.equal(duplicate.reused, true);
  assert.equal(duplicate.ticket, acquired.ticket);

  const refresh2 = memory.beginRefresh({ identity, navigation: "routing-policies" });
  memory.acceptRefresh(refresh2, [aliasFixture({ version: 2, mode: "availability_first" })]);
  const refresh3 = memory.beginRefresh({ identity, navigation: "routing-policies" });
  memory.acceptRefresh(refresh3, [aliasFixture({ version: 3, mode: "availability_first" })]);
  const duringSave = memory.snapshot().rows[0];
  assert.equal(duringSave.basePolicyVersion, 1);
  assert.equal(duringSave.draftMode, "cost_first");
  assert.equal(duringSave.busy, true);
  assert.equal(duringSave.operationGeneration, 1);
  assert.equal(duringSave.refreshGeneration, refresh3.generation);
  assert.equal(duringSave.sideAuthoritativeSnapshot.routing_policy.version, 3);

  const accepted = memory.acceptSuccess(
    acquired.ticket,
    {
      routing_policy: aliasFixture({ version: 2, mode: "cost_first" }).routing_policy,
      changed: true,
    },
    { identity, navigation: "routing-policies" },
  );
  assert.equal(accepted, true);
  const saved = memory.snapshot().rows[0];
  assert.equal(saved.busy, false);
  assert.equal(saved.dirty, false);
  assert.equal(saved.basePolicyVersion, 2);
  assert.equal(saved.draftMode, "cost_first");
  assert.equal(saved.operationGeneration, 1);
  assert.equal(saved.sideAuthoritativeSnapshot, null);
});

test("quality_cost config participates in dirty fingerprint CAS response and invalid-save gate", () => {
  const { memory, identity } = initializedMemory();
  assert.equal(memory.selectMode("alias_1", "quality_cost"), true);
  assert.equal(memory.selectQualityCostConfig("alias_1", {
    ...qualityCostConfig,
    quality_weight_bps: 5500,
    cost_weight_bps: 2500,
  }), true);
  const save = memory.acquireSave({
    identity,
    navigation: "routing-policies",
    modelAliasId: "alias_1",
  });
  assert.equal(save.state, "ok");
  assert.deepEqual(save.ticket.qualityCostConfig, {
    ...qualityCostConfig,
    quality_weight_bps: 5500,
    cost_weight_bps: 2500,
  });
  assert.match(save.ticket.fingerprint, /quality_weight_bps/);

  const wrongConfig = aliasFixture({ version: 2, mode: "quality_cost" }).routing_policy;
  assert.equal(memory.acceptSuccess(save.ticket, {
    changed: true,
    routing_policy: wrongConfig,
  }, { identity, navigation: "routing-policies" }), false);
  const expectedPolicy = aliasFixture({
    version: 2,
    mode: "quality_cost",
    config: save.ticket.qualityCostConfig,
  }).routing_policy;
  assert.equal(memory.acceptSuccess(save.ticket, {
    changed: true,
    routing_policy: expectedPolicy,
  }, { identity, navigation: "routing-policies" }), true);
  assert.deepEqual(memory.snapshot().rows[0].draftQualityCostConfig, save.ticket.qualityCostConfig);

  assert.equal(memory.selectQualityCostConfig("alias_1", {
    ...qualityCostConfig,
    latency_weight_bps: 1999,
  }), true);
  assert.equal(memory.acquireSave({
    identity,
    navigation: "routing-policies",
    modelAliasId: "alias_1",
  }).state, "blocked");
});

test("PATCH 200 must bind changed policy identity submitted mode and exact CAS version", () => {
  const invalidResponses = [
    { changed: "true" },
    { changed: false, routing_policy: { version: 1 } },
    { routing_policy: { routing_policy_id: "rpol_other" } },
    { routing_policy: { routing_mode: "availability_first" } },
    { routing_policy: { version: 1 } },
    { routing_policy: { version: 3 } },
    { routing_policy: { status: "disabled" } },
  ];
  for (const invalid of invalidResponses) {
    const { memory, identity } = initializedMemory();
    memory.selectMode("alias_1", "cost_first");
    const ticket = memory.acquireSave({
      identity,
      navigation: "routing-policies",
      modelAliasId: "alias_1",
    }).ticket;
    const policy = {
      ...aliasFixture({ version: 2, mode: "cost_first" }).routing_policy,
      ...(invalid.routing_policy ?? {}),
    };
    assert.equal(memory.acceptSuccess(
      ticket,
      { routing_policy: policy, changed: invalid.changed ?? true },
      { identity, navigation: "routing-policies" },
    ), false);
    // 调用方仍可把同一 ticket 转入统一失败态；拒绝坏 200 不能提前清掉操作身份。
    assert.equal(memory.snapshot().rows[0].busy, true);
  }
});

test("dirty refresh updates only side snapshot and 409 requires explicit rebase", () => {
  const { memory, identity } = initializedMemory();
  memory.selectMode("alias_1", "availability_first");
  const operation = memory.acquireSave({ identity, navigation: "routing-policies", modelAliasId: "alias_1" }).ticket;
  const conflict = new AuthApiError({
    status: 409,
    code: "model_alias.routing_policy_version_conflict",
    message: "conflict",
    requestId: "req_conflict",
  });
  conflict.currentRoutingPolicy = aliasFixture({ version: 2, mode: "cost_first" }).routing_policy;
  assert.equal(memory.acceptFailure(operation, conflict, { identity, navigation: "routing-policies" }), true);
  let row = memory.snapshot().rows[0];
  assert.equal(row.conflicted, true);
  assert.equal(row.basePolicyVersion, 1);
  assert.equal(row.draftMode, "availability_first");
  assert.equal(row.sideAuthoritativeSnapshot.routing_policy.version, 2);
  assert.equal(
    memory.acquireSave({ identity, navigation: "routing-policies", modelAliasId: "alias_1" }).state,
    "blocked",
  );

  assert.equal(memory.rebase("alias_1"), true);
  row = memory.snapshot().rows[0];
  assert.equal(row.basePolicyVersion, 2);
  assert.equal(row.draftMode, "cost_first");
  assert.equal(row.dirty, false);
  assert.equal(memory.selectMode("alias_1", "availability_first"), true);
  const retried = memory.acquireSave({ identity, navigation: "routing-policies", modelAliasId: "alias_1" });
  assert.equal(retried.ticket.baseVersion, 2);
  assert.equal(retried.ticket.routingMode, "availability_first");
  assert.equal(retried.ticket.operationGeneration, 2);
});

test("cancel after conflict or dirty refresh adopts latest authority instead of reviving v1", () => {
  for (const conflict of [false, true]) {
    const { memory, identity } = initializedMemory();
    memory.selectMode("alias_1", "availability_first");
    if (conflict) {
      const ticket = memory.acquireSave({
        identity,
        navigation: "routing-policies",
        modelAliasId: "alias_1",
      }).ticket;
      const error = new AuthApiError({ status: 409, requestId: "req_conflict_cancel" });
      error.currentRoutingPolicy = aliasFixture({ version: 2, mode: "cost_first" }).routing_policy;
      memory.acceptFailure(ticket, error, { identity, navigation: "routing-policies" });
    } else {
      const refresh = memory.beginRefresh({ identity, navigation: "routing-policies" });
      memory.acceptRefresh(refresh, [aliasFixture({ version: 2, mode: "cost_first" })]);
    }
    assert.equal(memory.cancel("alias_1"), true);
    const row = memory.snapshot().rows[0];
    assert.equal(row.basePolicyVersion, 2);
    assert.equal(row.draftMode, "cost_first");
    assert.equal(row.dirty, false);
    assert.equal(row.conflicted, false);
  }
});

test("cancel and identity invalidation abort tickets and reject late 200 or 409", () => {
  const { memory, identity } = initializedMemory();
  memory.selectMode("alias_1", "cost_first");
  const cancelled = memory.acquireSave({ identity, navigation: "routing-policies", modelAliasId: "alias_1" }).ticket;
  assert.equal(memory.cancel("alias_1"), true);
  assert.equal(cancelled.signal.aborted, true);
  assert.equal(
    memory.acceptSuccess(cancelled, { routing_policy: aliasFixture({ version: 2 }).routing_policy }, { identity, navigation: "routing-policies" }),
    false,
  );

  memory.selectMode("alias_1", "availability_first");
  const late = memory.acquireSave({ identity, navigation: "routing-policies", modelAliasId: "alias_1" }).ticket;
  memory.invalidate();
  assert.equal(late.signal.aborted, true);
  const nextContext = {
    ...adminContext,
    user: { ...adminContext.user, tenant_id: "tenant_2" },
    tenant: { tenant_id: "tenant_2", name: "Other" },
    session: { ...adminContext.session, session_id: "session_2" },
  };
  const nextIdentity = routingPolicyIdentitySnapshot(nextContext);
  const nextRefresh = memory.beginRefresh({ identity: nextIdentity, navigation: "routing-policies" });
  memory.acceptRefresh(nextRefresh, [{ ...aliasFixture(), tenant_id: "tenant_2", alias: "other-alias" }]);
  assert.equal(
    memory.acceptFailure(late, new AuthApiError({ status: 409, requestId: "late" }), {
      identity: nextIdentity,
      navigation: "routing-policies",
    }),
    false,
  );
  assert.equal(memory.snapshot().rows[0].alias.alias, "other-alias");
});

test("routing policy renderer gives admin editor developer readonly and viewer no DOM", () => {
  const { memory } = initializedMemory();
  const state = memory.snapshot();
  state.rows[0].alias.alias = "<script>alias</script>";
  state.rows[0].alias.routing_policy.candidates[1].provider_code = "<img src=x>";
  const developerContext = withPermissionSnapshot(
    { ...adminContext, user: { ...adminContext.user, role: "developer" } },
    [PERMISSIONS.ROUTING_READ],
  );
  const viewerContext = withPermissionSnapshot(
    { ...adminContext, user: { ...adminContext.user, role: "viewer" } },
    [PERMISSIONS.CONSOLE_BASIC_READ],
  );

  const adminHtml = renderRoutingPolicyPanel(state, adminContext);
  const developerHtml = renderRoutingPolicyPanel(state, developerContext);
  const viewerHtml = renderRoutingPolicyPanel(state, viewerContext);
  assert.match(adminHtml, /data-routing-policy-form/);
  assert.match(adminHtml, /成本优先/);
  assert.match(adminHtml, /质量成本/);
  assert.match(adminHtml, /qualityWeightPercent/);
  assert.match(adminHtml, /minimumSampleCount/);
  assert.match(adminHtml, /备用角色/);
  assert.match(adminHtml, /&lt;script&gt;alias&lt;\/script&gt;/);
  assert.doesNotMatch(adminHtml, /<script>alias<\/script>|<img src=x>|必然被选中|已触发 Fallback/);
  assert.match(developerHtml, /路由策略/);
  assert.doesNotMatch(developerHtml, /<form|<button|data-routing-policy-form/);
  assert.equal(viewerHtml, "");
});

test("developer start dispatches PAGE-067 only from authorized active navigation", () => {
  const { memory } = initializedMemory();
  const baseOptions = {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    routingPoliciesState: memory.snapshot(),
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "routing-policies",
        navigation_items: [{
          id: "routing-policies",
          label: "路由策略",
          group: "配置",
          target_section: "routing-policies",
          description: "Routing",
          order: 65,
        }],
      },
    },
  };
  const adminContainer = { innerHTML: "" };
  renderDeveloperStart(adminContainer, { ...baseOptions, authContext: adminContext });
  assert.match(adminContainer.innerHTML, /routing-policies-panel/);
  assert.match(adminContainer.innerHTML, /data-routing-policy-form/);

  const viewerContainer = { innerHTML: "" };
  renderDeveloperStart(viewerContainer, {
    ...baseOptions,
    authContext: withPermissionSnapshot(
      { ...adminContext, user: { ...adminContext.user, role: "viewer" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
  });
  assert.doesNotMatch(viewerContainer.innerHTML, /routing-policies-panel|data-routing-policy-form/);
});

test("PAGE-072 production loading transition copies authorized shell state and fails closed", () => {
  const consoleShellState = {
    state: "ok",
    activeNavigationItemId: "overview",
    navigation: {
      active_navigation_item_id: "overview",
      navigation_items: [
        {
          id: "overview",
          label: "概览",
          group: "总览",
          target_section: "overview",
          description: "Overview",
          order: 10,
        },
        {
          id: "routing-policies",
          label: "路由策略",
          group: "配置",
          target_section: "routing-policies",
          description: "Routing",
          order: 65,
        },
      ],
    },
  };
  const originalSnapshot = structuredClone(consoleShellState);

  for (const role of ["admin", "developer"]) {
    const container = { innerHTML: "" };
    const projected = renderRoutingPoliciesLoading(container, {
      authContext: withPermissionSnapshot(
        { ...adminContext, user: { ...adminContext.user, role } },
        [PERMISSIONS.ROUTING_READ],
      ),
      consoleShellState,
    });
    assert.ok(projected);
    assert.notEqual(projected, consoleShellState);
    assert.notEqual(projected.navigation, consoleShellState.navigation);
    assert.equal(projected.activeNavigationItemId, "routing-policies");
    assert.equal(projected.navigation.active_navigation_item_id, "routing-policies");
    assert.match(container.innerHTML, /data-active-navigation="routing-policies"/);
    assert.match(container.innerHTML, /routing-policies-panel/);
    assert.match(container.innerHTML, /id="routing-policies-title">路由策略/);
    assert.match(container.innerHTML, /等待 \/model-aliases 响应/);
    assert.doesNotMatch(
      container.innerHTML,
      /id="login-form"|data-routing-policy-row|data-cache-affinity-row|data-routing-policy-form|data-cache-affinity-form/,
    );
  }
  assert.deepEqual(consoleShellState, originalSnapshot);

  const deniedCases = [
    {
      authContext: withPermissionSnapshot(
        { ...adminContext, user: { ...adminContext.user, role: "viewer" } },
        [PERMISSIONS.CONSOLE_BASIC_READ],
      ),
      consoleShellState,
    },
    {
      authContext: adminContext,
      consoleShellState: {
        ...consoleShellState,
        navigation: {
          ...consoleShellState.navigation,
          navigation_items: consoleShellState.navigation.navigation_items.slice(0, 1),
        },
      },
    },
    {
      authContext: adminContext,
      consoleShellState: {
        ...consoleShellState,
        navigation: {
          ...consoleShellState.navigation,
          navigation_items: consoleShellState.navigation.navigation_items.map((item) => (
            item.id === "routing-policies" ? { ...item, target_section: "overview" } : item
          )),
        },
      },
    },
  ];
  for (const denied of deniedCases) {
    const container = { innerHTML: "existing-safe-page" };
    assert.equal(renderRoutingPoliciesLoading(container, denied), null);
    assert.equal(container.innerHTML, "existing-safe-page");
  }
});

test("PAGE-072 navigation owner rejects a newer navigation or replacement Session", () => {
  const owner = Object.freeze({ generation: 7, token: "session_token_owner" });
  assert.equal(isConsoleNavigationOwnerCurrent(owner, {
    generation: 7,
    token: "session_token_owner",
  }), true);
  for (const current of [
    { generation: 8, token: "session_token_owner" },
    { generation: 7, token: "session_token_replacement" },
    { generation: 7, token: null },
  ]) {
    assert.equal(isConsoleNavigationOwnerCurrent(owner, current), false);
  }
  for (const invalidOwner of [
    null,
    { generation: 0, token: "session_token_owner" },
    { generation: 7, token: "" },
  ]) {
    assert.equal(isConsoleNavigationOwnerCurrent(invalidOwner, {
      generation: 7,
      token: "session_token_owner",
    }), false);
  }
});

test("main wires PAGE-072 loading after persisted navigation telemetry and before full refresh", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const start = source.indexOf("async function switchConsoleNavigation");
  const end = source.indexOf("\n/**", start);
  assert.ok(start >= 0 && end > start);
  const transition = source.slice(start, end);
  const ownerIndex = transition.indexOf("const navigationOwner = Object.freeze");
  const preferenceIndex = transition.indexOf("await updateConsolePreferences");
  const preferenceGateIndex = transition.indexOf("if (!navigationOwnerCurrent())", preferenceIndex);
  const pageViewIndex = transition.indexOf("await recordConsolePageView");
  const pageViewGateIndex = transition.indexOf("if (!navigationOwnerCurrent())", pageViewIndex);
  const loadingIndex = transition.indexOf("renderRoutingPoliciesLoading(app");
  // 其他治理页也有同名重绑；PAGE-072 门禁必须锁定自己的 loading 之后那一次，避免误命中前序页面分支。
  const rebindIndex = transition.indexOf("bindAuthenticatedEvents(projectedState)", loadingIndex);
  const refreshGateIndex = transition.indexOf("if (!navigationOwnerCurrent())", rebindIndex);
  const refreshCallIndex = transition.indexOf("const refreshPromise = refresh();", refreshGateIndex);
  const refreshOwnerIndex = transition.indexOf("errorOwner = Object.freeze", refreshCallIndex);
  const refreshAwaitIndex = transition.indexOf("await refreshPromise;", refreshOwnerIndex);
  const catchIndex = transition.indexOf("} catch (error)");
  const catchGateIndex = transition.indexOf("if (!errorOwnerCurrent())", catchIndex);
  assert.ok(
    ownerIndex >= 0
      && ownerIndex < preferenceIndex
      && preferenceIndex < preferenceGateIndex
      && preferenceGateIndex < pageViewIndex
      && pageViewIndex < pageViewGateIndex
      && pageViewGateIndex < loadingIndex
      && loadingIndex < rebindIndex
      && rebindIndex < refreshGateIndex
      && refreshGateIndex < refreshCallIndex
      && refreshCallIndex < refreshOwnerIndex
      && refreshOwnerIndex < refreshAwaitIndex
      && refreshAwaitIndex < catchIndex
      && catchIndex < catchGateIndex,
  );
  assert.match(
    source,
    /switchConsoleNavigation\(navigationButton, consoleShellState\)/,
  );
});

test("routing policy CSS has bounded desktop tablet and mobile layouts", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.routing-policy-row\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /@media \(max-width: 1100px\)[\s\S]*\.routing-policy-row:not\(\.routing-policy-head\)/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.routing-policy-buttons button/);
  assert.match(css, /@media \(max-width: 390px\)[\s\S]*\.routing-policy-row:not\(\.routing-policy-head\)/);
  assert.match(css, /\.routing-quality-cost-grid\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /\.routing-quality-weight-total\.is-valid/);
  assert.match(css, /overflow-wrap: anywhere/);
});
