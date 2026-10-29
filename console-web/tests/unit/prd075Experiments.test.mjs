/**
 * PRD-075 PAGE-105 API 投影、tenant/RBAC owner、异步生命周期和响应式界面专项测试。
 * 测试数据仅包含本地合成 ID 与整数统计，不含 Token、Prompt、Response 或 Provider Endpoint。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { experimentCreateRequestFromFormData } from "../../src/features/experiments/bindExperiments.js";
import {
  createExperimentPageState,
  experimentPageOwner,
} from "../../src/features/experiments/experimentPageState.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderExperiments } from "../../src/features/experiments/renderExperiments.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createExperiment,
  listExperiments,
  normalizeCreateExperimentRequest,
  normalizeExperimentDetailResponse,
  normalizeExperimentListResponse,
  normalizeExperimentResultsResponse,
} from "../../src/lib/experiments.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 固定实验 ID 和创建权限组合用于证明 read 与 composite write 能力彼此独立。
const EXPERIMENT_ID = `exp_${"a".repeat(32)}`;
const OTHER_EXPERIMENT_ID = `exp_${"b".repeat(32)}`;
const CREATE_PERMISSIONS = [
  PERMISSIONS.EXPERIMENT_READ,
  PERMISSIONS.EXPERIMENT_WRITE,
  PERMISSIONS.PROJECT_READ,
  PERMISSIONS.ENVIRONMENT_READ,
  PERMISSIONS.ROUTING_READ,
  PERMISSIONS.MODEL_READ,
];

/** 构造包含完整 Session、tenant 镜像和数据库权限快照的认证上下文。 */
function authContext(permissions = CREATE_PERMISSIONS, tenantId = "tenant_experiment_a") {
  // 权限集合只进入合成 owner，不代表前端可以替代后端 RBAC 校验。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, slug: "experiment", display_name: "Experiment tenant" },
    user: {
      tenant_id: tenantId,
      user_id: "user_experiment_a",
      email: "experiment@example.test",
      role: "developer",
    },
    session: { session_id: "session_experiment_a", session_revision: 1 },
  }, permissions, { permissionRevision: 11, authorizationFingerprint: "experiment_authz_a" });
}

/** 构造后端已授权的 experiments 导航快照，并与 auth/me 权限元数据精确镜像。 */
function shellState(context = authContext()) {
  return {
    state: "ok",
    activeNavigationItemId: "experiments",
    navigation: {
      active_navigation_item_id: "experiments",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "experiments",
        target_section: "experiments",
        label: "模型评测",
        group: "configuration",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.EXPERIMENT_READ],
      }],
    },
  };
}

/** 构造内部配额、计数和单向时间矩阵一致的 ExperimentInfo。 */
function experimentInfo(overrides = {}) {
  return {
    experiment_id: EXPERIMENT_ID,
    name: "August quality comparison",
    project_id: "project_a",
    environment_id: "environment_a",
    model_alias_id: "alias_a",
    model_a: {
      variant: "a",
      model_id: "model_a",
      provider_id: "provider_a",
      provider_code: "deepseek_official",
      allocation_bps: 5000,
      target_count: 5,
      assigned_count: 5,
    },
    model_b: {
      variant: "b",
      model_id: "model_b",
      provider_id: "provider_b",
      provider_code: "zhipu_official",
      allocation_bps: 5000,
      target_count: 5,
      assigned_count: 5,
    },
    target_sample_count: 10,
    traffic_sample_bps: 2500,
    sampling_duration_seconds: 86400,
    feedback_collection_enabled: true,
    feedback_window_seconds: 86400,
    algorithm_version: 1,
    status: "completed",
    assigned_count: 10,
    facts_version: 37,
    started_at: "2026-08-09T01:00:00Z",
    sampling_deadline: "2026-08-10T01:00:00Z",
    sampling_ended_at: "2026-08-09T01:01:00Z",
    feedback_deadline: "2026-08-10T01:01:00Z",
    settlement_deadline: "2026-08-10T02:01:00Z",
    completed_at: "2026-08-10T02:01:00Z",
    created_at: "2026-08-09T01:00:00Z",
    updated_at: "2026-08-10T02:01:00Z",
    ...overrides,
  };
}

/** 构造首次创建响应应返回的 active、零分配、零事实版本实验。 */
function activeExperimentInfo(overrides = {}) {
  const base = experimentInfo();
  return experimentInfo({
    model_a: { ...base.model_a, assigned_count: 0 },
    model_b: { ...base.model_b, assigned_count: 0 },
    status: "active",
    assigned_count: 0,
    facts_version: 0,
    sampling_ended_at: null,
    feedback_deadline: null,
    settlement_deadline: null,
    completed_at: null,
    updated_at: base.created_at,
    ...overrides,
  });
}

/** 构造统计分区与 half-up 派生值一致的单个实验 arm 结果。 */
function armResult(variant, overrides = {}) {
  return {
    variant,
    model_id: variant === "a" ? "model_a" : "model_b",
    provider_id: variant === "a" ? "provider_a" : "provider_b",
    provider_code: variant === "a" ? "deepseek_official" : "zhipu_official",
    assigned_count: 5,
    gateway_success_count: 4,
    gateway_failure_count: 1,
    unresolved_count: 0,
    gateway_success_rate_bps: 8000,
    exposed_count: 5,
    not_exposed_count: 0,
    exposure_unknown_count: 0,
    quality_success_count: 4,
    quality_failure_count: 1,
    quality_success_rate_bps: 8000,
    latency_sample_count: 5,
    total_latency_ms: 500,
    average_latency_micros: 100000,
    p95_latency_ms: 140,
    settlement_recorded_count: 5,
    settlement_not_exposed_count: 0,
    settlement_unavailable_count: 0,
    settlement_pending_count: 0,
    gross_cost_cny_micros: 250000,
    cost_known_count: 5,
    cost_unknown_count: 0,
    average_cost_cny_micros: 50000,
    feedback_collection_enabled: true,
    feedback_count: 2,
    rating_sum: 9,
    average_rating_milli: 4500,
    accepted_count: 1,
    adoption_rate_bps: 5000,
    ...overrides,
  };
}

/** 构造 completed 状态下 pending 为零且包含两个唯一 variant 的结果 envelope。 */
function resultsPayload(overrides = {}) {
  return {
    experiment_id: EXPERIMENT_ID,
    status: "completed",
    is_final: true,
    completion_reason: "target_reached",
    facts_version: 37,
    facts_through_at: "2026-08-10T02:01:00Z",
    next_state_at: null,
    pending_execution_count: 0,
    pending_settlement_count: 0,
    results: [armResult("a"), armResult("b")],
    request_id: "request_results_a",
    trace_id: "trace_results_a",
    ...overrides,
  };
}

/** 构造只含 active Project、Environment、Alias 与 Chat 模型的创建候选。 */
function createOptionsPayload(overrides = {}) {
  return {
    projects: [{ project_id: "project_a", name: "Project A", status: "active" }],
    environments: [{
      environment_id: "environment_a",
      name: "Production",
      code: "prod",
      status: "active",
    }],
    modelAliases: [{
      model_alias_id: "alias_a",
      alias: "chat",
      environment: "prod",
      status: "active",
    }],
    models: [
      {
        model_id: "model_a",
        display_name: "Model A",
        provider_id: "provider_a",
        status: "active",
        capabilities: { chat: true },
      },
      {
        model_id: "model_b",
        display_name: "Model B",
        provider_id: "provider_b",
        status: "active",
        capabilities: { chat: true },
      },
    ],
    ...overrides,
  };
}

/** 构造带强制 no-store/private 响应头的本地 JSON Response，用于验证浏览器缓存边界。 */
function jsonResponse(payload, { status = 200, cacheControl = "no-store, private" } = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": cacheControl },
  });
}

test("strict experiment projections reject extra or internally inconsistent fields", () => {
  const list = normalizeExperimentListResponse({
    experiments: [experimentInfo()],
    next_cursor: "private_cursor_value",
    request_id: "request_list_a",
    trace_id: "trace_list_a",
  });
  assert.equal(list?.experiments.length, 1);
  assert.equal(normalizeExperimentListResponse({ ...list, provider_base_url: "hidden" }), null);
  assert.equal(normalizeExperimentListResponse({
    experiments: [experimentInfo(), experimentInfo()],
    next_cursor: null,
    request_id: "request_list_duplicate",
    trace_id: "trace_list_duplicate",
  }), null);
  assert.equal(normalizeExperimentListResponse({
    experiments: [experimentInfo({
      model_b: { ...experimentInfo().model_b, allocation_bps: 4900 },
    })],
    next_cursor: null,
    request_id: "request_list_bad_allocation",
    trace_id: "trace_list_bad_allocation",
  }), null);

  const detail = normalizeExperimentDetailResponse({
    experiment: experimentInfo(),
    request_id: "request_detail_a",
    trace_id: "trace_detail_a",
  }, EXPERIMENT_ID);
  assert.equal(detail?.experiment.experiment_id, EXPERIMENT_ID);
  assert.equal(normalizeExperimentDetailResponse({
    experiment: experimentInfo({ assigned_count: 9 }),
    request_id: "request_detail_a",
    trace_id: "trace_detail_a",
  }), null);
  assert.equal(normalizeExperimentDetailResponse({
    experiment: experimentInfo({ completed_at: "2026-08-10T00:30:00Z" }),
    request_id: "request_detail_early_completion",
    trace_id: "trace_detail_early_completion",
  }), null);
  assert.equal(normalizeExperimentDetailResponse({
    experiment: experimentInfo({ updated_at: "2026-08-10T01:30:00Z" }),
    request_id: "request_detail_stale_update",
    trace_id: "trace_detail_stale_update",
  }), null);
  assert.equal(normalizeExperimentDetailResponse({
    experiment: experimentInfo({ created_at: "2026-08-09T01:00:00.000001Z" }),
    request_id: "request_detail_microsecond_drift",
    trace_id: "trace_detail_microsecond_drift",
  }), null);
  assert.equal(normalizeExperimentDetailResponse({
    experiment: experimentInfo({ updated_at: "2026-02-30T02:01:00Z" }),
    request_id: "request_detail_invalid_calendar",
    trace_id: "trace_detail_invalid_calendar",
  }), null);

  assert.equal(normalizeExperimentResultsResponse(resultsPayload())?.results.length, 2);
  assert.equal(normalizeExperimentResultsResponse(resultsPayload({
    results: [armResult("a"), armResult("a")],
  })), null);
  assert.equal(normalizeExperimentResultsResponse(resultsPayload({
    results: [armResult("a", { gateway_success_rate_bps: 8100 }), armResult("b")],
  })), null);
  assert.equal(normalizeExperimentResultsResponse(resultsPayload({
    results: [armResult("a", { settlement_pending_count: 1 }), armResult("b")],
  })), null);
  assert.equal(normalizeExperimentResultsResponse(resultsPayload({
    results: [armResult("a", {
      gateway_failure_count: 0,
      unresolved_count: 1,
      gateway_success_rate_bps: 10000,
    }), armResult("b")],
  })), null);
});

test("create body uses integer bps and enforces distinct models and feedback pairing", () => {
  const data = new Map(Object.entries({
    name: "August quality comparison",
    project_id: "project_a",
    environment_id: "environment_a",
    model_alias_id: "alias_a",
    model_a_id: "model_a",
    model_b_id: "model_b",
    target_sample_count: "10",
    traffic_sample_bps: "2500",
    model_a_allocation_bps: "5000",
    sampling_duration_seconds: "86400",
    feedback_collection_enabled: "on",
    feedback_window_seconds: "86400",
  }));
  const request = experimentCreateRequestFromFormData(data);
  assert.equal(request.traffic_sample_bps, 2500);
  assert.equal(request.feedback_collection_enabled, true);
  assert.throws(
    () => normalizeCreateExperimentRequest({ ...request, model_b_id: "model_a" }),
    /实验配置不合法/,
  );
  assert.throws(
    () => normalizeCreateExperimentRequest({ ...request, traffic_sample_bps: 25.5 }),
    /实验配置不合法/,
  );
});

test("owner exposes read separately and creates only with the full composite permission set", () => {
  const full = authContext();
  const owner = experimentPageOwner(full, shellState(full));
  assert.equal(owner?.canCreate, true);
  assert.equal(owner?.tenantId, "tenant_experiment_a");

  const readOnly = authContext([PERMISSIONS.EXPERIMENT_READ]);
  assert.equal(experimentPageOwner(readOnly, shellState(readOnly))?.canCreate, false);
  assert.equal(experimentPageOwner(readOnly, shellState(readOnly), { mode: "create" }), null);

  for (const permission of CREATE_PERMISSIONS.filter(
    (code) => code !== PERMISSIONS.EXPERIMENT_READ,
  )) {
    const narrowed = authContext(CREATE_PERMISSIONS.filter((code) => code !== permission));
    assert.equal(experimentPageOwner(narrowed, shellState(narrowed))?.canCreate, false);
  }

  const mismatchedTenant = {
    ...full,
    tenant: { ...full.tenant, tenant_id: "tenant_experiment_b" },
  };
  assert.equal(experimentPageOwner(mismatchedTenant, shellState(full)), null);
  const missingTenantMirror = { ...full, tenant: undefined };
  assert.equal(experimentPageOwner(missingTenantMirror, shellState(full)), null);
  const incompleteNavigation = shellState(full);
  delete incompleteNavigation.navigation.navigation_items[0].visible;
  assert.equal(experimentPageOwner(full, incompleteNavigation), null);
});

test("state keeps cursor and authorization owner private and aborts old identity", () => {
  const context = authContext();
  const owner = experimentPageOwner(context, shellState(context));
  const state = createExperimentPageState();
  assert.equal(state.enter(owner), true);
  const operation = state.beginList(owner, 7);
  assert.equal(state.acceptList(operation, {
    experiments: [experimentInfo()],
    next_cursor: "private_cursor_value",
    request_id: "request_list_a",
    trace_id: "trace_list_a",
  }, owner, 7, createOptionsPayload()), true);
  const snapshot = state.snapshot();
  assert.equal(snapshot.canNext, true);
  assert.equal(snapshot.createOptionsState, "ready");
  assert.equal(JSON.stringify(snapshot).includes("private_cursor_value"), false);
  assert.equal(JSON.stringify(snapshot).includes("experiment_authz_a"), false);
  state.clear();
  assert.equal(operation.controller.signal.aborted, true);
  assert.equal(state.snapshot().experiments.length, 0);
});

test("owner drift clears state before abort and late list callbacks cannot revive the old tenant", () => {
  const context = authContext();
  const owner = experimentPageOwner(context, shellState(context));
  const events = [];
  let state;
  state = createExperimentPageState({
    clearDom: () => events.push({
      event: "clear-dom",
      experiments: state.snapshot().experiments.length,
      canCreate: state.snapshot().canCreate,
    }),
  });
  state.enter(owner);
  const initialOperation = state.beginList(owner, 10);
  state.acceptList(initialOperation, {
    experiments: [experimentInfo()],
    next_cursor: null,
    request_id: "request_initial",
    trace_id: "trace_initial",
  }, owner, 10, createOptionsPayload());
  state.finishList(initialOperation);
  const oldOperation = state.beginList(owner, 11);
  oldOperation.controller.signal.addEventListener("abort", () => events.push({
    event: "abort",
    experiments: state.snapshot().experiments.length,
    canCreate: state.snapshot().canCreate,
  }));

  const narrowedContext = authContext(
    CREATE_PERMISSIONS.filter((code) => code !== PERMISSIONS.MODEL_READ),
  );
  const narrowedOwner = experimentPageOwner(narrowedContext, shellState(narrowedContext));
  assert.equal(state.enter(narrowedOwner), true);
  assert.deepEqual(events, [
    { event: "clear-dom", experiments: 0, canCreate: false },
    { event: "abort", experiments: 0, canCreate: false },
  ]);
  assert.equal(state.snapshot().canCreate, false);
  assert.equal(state.acceptList(
    oldOperation,
    {
      experiments: [experimentInfo()],
      next_cursor: null,
      request_id: "request_late",
      trace_id: "trace_late",
    },
    owner,
    11,
    createOptionsPayload(),
  ), false);
  assert.equal(state.snapshot().experiments.length, 0);

  assert.equal(state.enter(null), false);
  assert.equal(state.snapshot().canCreate, false);
  assert.equal(JSON.stringify(state.snapshot()).includes("tenant_experiment_a"), false);
});

test("detail and result acceptance binds facts version, status, arm owner, count, and feedback mode", () => {
  const context = authContext();
  const owner = experimentPageOwner(context, shellState(context));
  const state = createExperimentPageState();
  state.enter(owner);
  const listOperation = state.beginList(owner, 3);
  state.acceptList(listOperation, {
    experiments: [experimentInfo()],
    next_cursor: null,
    request_id: "request_list_detail",
    trace_id: "trace_list_detail",
  }, owner, 3, createOptionsPayload());
  state.finishList(listOperation);
  assert.equal(state.selectExperiment(EXPERIMENT_ID), true);

  const mismatch = state.beginDetail(owner, 3);
  const mismatchResults = state.beginResults(owner, 3);
  assert.notEqual(mismatch.controller, mismatchResults.controller);
  assert.equal(state.acceptDetail(
    mismatch,
    mismatchResults,
    { experiment: experimentInfo(), request_id: "request_detail", trace_id: "trace_detail" },
    resultsPayload({ facts_version: 38 }),
    owner,
  ), false);
  assert.equal(state.snapshot().detailState, "error");
  assert.equal(state.snapshot().detailError.code, "experiment.statistics_unavailable");
  state.finishDetail(mismatch);
  state.finishResults(mismatchResults);

  const current = state.beginDetail(owner, 3);
  const currentResults = state.beginResults(owner, 3);
  assert.equal(state.acceptDetail(
    current,
    currentResults,
    { experiment: experimentInfo(), request_id: "request_detail", trace_id: "trace_detail" },
    resultsPayload(),
    owner,
  ), true);
  assert.equal(state.snapshot().resultsState, "ready");
  state.finishDetail(current);
  state.finishResults(currentResults);

  const failed = state.beginDetail(owner, 3);
  const failedResults = state.beginResults(owner, 3);
  assert.equal(state.rejectDetail(failed, failedResults, {
    code: "experiment.statistics_unavailable",
    message: "private database diagnosis",
    requestId: "request_statistics_failed",
  }, owner), true);
  assert.equal(failed.controller.signal.aborted, true);
  assert.equal(failedResults.controller.signal.aborted, true);
  assert.equal(state.snapshot().detailError.message, "实验统计暂不可用");
  assert.doesNotMatch(state.snapshot().detailError.message, /database|private/i);
});

test("background statistics 503 retains only the same-owner final result and aborts both lanes", () => {
  const context = authContext();
  const owner = experimentPageOwner(context, shellState(context));
  const state = createExperimentPageState();
  state.enter(owner);
  const listOperation = state.beginList(owner, 20);
  state.acceptList(listOperation, {
    experiments: [experimentInfo()],
    next_cursor: null,
    request_id: "request_background_list",
    trace_id: "trace_background_list",
  }, owner, 20, createOptionsPayload());
  state.finishList(listOperation);
  assert.equal(state.selectExperiment(EXPERIMENT_ID), true);
  const initialDetail = state.beginDetail(owner, 20);
  const initialResults = state.beginResults(owner, 20);
  assert.equal(state.acceptDetail(
    initialDetail,
    initialResults,
    { experiment: experimentInfo(), request_id: "request_final_detail" },
    resultsPayload(),
    owner,
  ), true);
  state.finishDetail(initialDetail);
  state.finishResults(initialResults);
  const finalSnapshot = state.snapshot();

  const backgroundDetail = state.beginDetail(owner, 21);
  const backgroundResults = state.beginResults(owner, 21);
  assert.equal(state.rejectDetail(
    backgroundDetail,
    backgroundResults,
    new AuthApiError({
      status: 503,
      code: "experiment.statistics_unavailable",
      message: "private transient detail",
      requestId: "request_background_statistics",
    }),
    owner,
    { preserveFinalOnStatisticsUnavailable: true },
  ), true);
  assert.equal(backgroundDetail.controller.signal.aborted, true);
  assert.equal(backgroundResults.controller.signal.aborted, true);
  assert.equal(state.isDetailCurrent(backgroundDetail, owner), false);
  assert.equal(state.isResultsCurrent(backgroundResults, owner), false);
  assert.strictEqual(state.snapshot().experiment, finalSnapshot.experiment);
  assert.strictEqual(state.snapshot().results, finalSnapshot.results);
  assert.equal(state.snapshot().detailState, "ready");
  assert.equal(state.snapshot().resultsState, "ready");
  assert.equal(state.snapshot().detailError, null);
  state.finishDetail(backgroundDetail);
  state.finishResults(backgroundResults);

  // 同一瞬时错误若来自显式刷新，仍必须可见，不能无限掩盖统计服务故障。
  const explicitDetail = state.beginDetail(owner, 22);
  const explicitResults = state.beginResults(owner, 22);
  assert.equal(state.rejectDetail(
    explicitDetail,
    explicitResults,
    new AuthApiError({
      status: 503,
      code: "experiment.statistics_unavailable",
      requestId: "request_explicit_statistics",
    }),
    owner,
  ), true);
  assert.equal(state.snapshot().detailState, "error");
  assert.equal(state.snapshot().results, null);
  state.finishDetail(explicitDetail);
  state.finishResults(explicitResults);

  // 后台保留标记不能冻结仍在变化的 provisional 结果。
  state.clear();
  state.enter(owner);
  const active = activeExperimentInfo();
  const activeList = state.beginList(owner, 23);
  state.acceptList(activeList, {
    experiments: [active],
    next_cursor: null,
    request_id: "request_active_list",
    trace_id: "trace_active_list",
  }, owner, 23, createOptionsPayload());
  state.finishList(activeList);
  assert.equal(state.selectExperiment(EXPERIMENT_ID), true);
  const activeDetail = state.beginDetail(owner, 23);
  const activeResults = state.beginResults(owner, 23);
  assert.equal(state.acceptDetail(
    activeDetail,
    activeResults,
    { experiment: active, request_id: "request_active_detail" },
    resultsPayload({
      status: "active",
      is_final: false,
      completion_reason: null,
      facts_version: 0,
      next_state_at: active.sampling_deadline,
      results: [
        armResult("a", { assigned_count: 0 }),
        armResult("b", { assigned_count: 0 }),
      ],
    }),
    owner,
  ), true);
  state.finishDetail(activeDetail);
  state.finishResults(activeResults);
  const provisionalDetail = state.beginDetail(owner, 24);
  const provisionalResults = state.beginResults(owner, 24);
  assert.equal(state.rejectDetail(
    provisionalDetail,
    provisionalResults,
    new AuthApiError({
      status: 503,
      code: "experiment.statistics_unavailable",
      requestId: "request_provisional_statistics",
    }),
    owner,
    { preserveFinalOnStatisticsUnavailable: true },
  ), true);
  assert.equal(state.snapshot().detailState, "error");
  assert.equal(state.snapshot().results, null);
});

test("selecting another experiment aborts independent detail and results lanes", () => {
  const context = authContext();
  const owner = experimentPageOwner(context, shellState(context));
  const state = createExperimentPageState();
  state.enter(owner);
  const listOperation = state.beginList(owner, 4);
  state.acceptList(listOperation, {
    experiments: [experimentInfo(), experimentInfo({ experiment_id: OTHER_EXPERIMENT_ID })],
    next_cursor: null,
    request_id: "request_list_switch",
    trace_id: "trace_list_switch",
  }, owner, 4, createOptionsPayload());
  state.finishList(listOperation);
  assert.equal(state.selectExperiment(EXPERIMENT_ID), true);
  const detailOperation = state.beginDetail(owner, 4);
  const resultsOperation = state.beginResults(owner, 4);

  assert.equal(state.selectExperiment(OTHER_EXPERIMENT_ID), true);
  assert.equal(detailOperation.controller.signal.aborted, true);
  assert.equal(resultsOperation.controller.signal.aborted, true);
  assert.equal(state.snapshot().selectedExperimentId, OTHER_EXPERIMENT_ID);
});

test("create option failures remain visible and cannot create until active Chat candidates recover", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = experimentPageOwner(context, shell);
  const state = createExperimentPageState();
  state.enter(owner);
  const operation = state.beginList(owner, 5);
  state.acceptList(operation, {
    experiments: [],
    next_cursor: null,
    request_id: "request_list_options",
    trace_id: "trace_list_options",
  }, owner, 5, createOptionsPayload({ models: null, requestId: "request_models_failed" }));
  state.finishList(operation);
  assert.equal(state.snapshot().createOptionsState, "error");
  assert.equal(state.openCreate(owner), true);
  assert.equal(state.beginCreate(owner), null);
  const html = renderExperiments(state.snapshot(), context, shell);
  assert.match(html, /创建选项暂不可用/);
  assert.doesNotMatch(html, /request_models_failed/);
  assert.match(html, /data-experiment-create-retry/);

  const incompatibleOperation = state.beginList(owner, 6);
  state.acceptList(incompatibleOperation, {
    experiments: [],
    next_cursor: null,
    request_id: "request_list_incompatible_scope",
    trace_id: "trace_list_incompatible_scope",
  }, owner, 6, createOptionsPayload({
    modelAliases: [{
      model_alias_id: "alias_other_environment",
      alias: "other-chat",
      environment: "staging",
      status: "active",
    }],
  }));
  state.finishList(incompatibleOperation);
  assert.equal(state.snapshot().createOptionsState, "empty");
  assert.equal(state.snapshot().createOptionsError.code, "experiment.create_options_empty");
});

test("experiment client disables browser cache and cookies while hiding hostile server messages", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      experiments: [experimentInfo()],
      next_cursor: null,
      request_id: "request_fetch_list",
      trace_id: "trace_fetch_list",
    });
  };
  const listed = await listExperiments({
    controlApiUrl: "http://control.local",
    token: "local-test-jwt",
  });
  assert.equal(listed.experiments.length, 1);
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.headers.Authorization, "Bearer local-test-jwt");
  assert.doesNotMatch(calls[0].url, /tenant/);

  globalThis.fetch = async () => jsonResponse({
    experiments: [experimentInfo()],
    next_cursor: null,
    request_id: "request_cacheable",
    trace_id: "trace_cacheable",
  }, { cacheControl: "no-store" });
  await assert.rejects(
    () => listExperiments({ controlApiUrl: "http://control.local", token: "local-test-jwt" }),
    (error) => error.code === "experiment.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse({
    experiments: [experimentInfo()],
    next_cursor: null,
    request_id: "request_cache_directive_parameters",
    trace_id: "trace_cache_directive_parameters",
  }, { cacheControl: "no-store=false, private=false" });
  await assert.rejects(
    () => listExperiments({ controlApiUrl: "http://control.local", token: "local-test-jwt" }),
    (error) => error.code === "experiment.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse({
    code: "auth.permission_denied",
    message: "private provider key and raw response must never render",
    diagnosis: { sql: "private" },
    request_id: "request_permission_revoked",
    trace_id: "trace_permission_revoked",
  }, { status: 403 });
  await assert.rejects(
    () => listExperiments({
      controlApiUrl: "http://control.local",
      token: "local-test-jwt",
    }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "request_permission_revoked");
      assert.equal(error.message, "当前账号没有模型评测权限");
      assert.doesNotMatch(error.message, /provider key|raw response|private/i);
      return true;
    },
  );
});

test("create client requires HTTP 201 and sends exact body with one memory-only idempotency key", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      experiment: activeExperimentInfo(),
      idempotent_replay: false,
      request_id: "request_create_http",
      trace_id: "trace_create_http",
    }, { status: 201 });
  };
  const request = normalizeCreateExperimentRequest({
    name: "August quality comparison",
    project_id: "project_a",
    environment_id: "environment_a",
    model_alias_id: "alias_a",
    model_a_id: "model_a",
    model_b_id: "model_b",
    target_sample_count: 10,
    traffic_sample_bps: 2500,
    model_a_allocation_bps: 5000,
    sampling_duration_seconds: 86400,
    feedback_collection_enabled: true,
    feedback_window_seconds: 86400,
  });
  const created = await createExperiment({
    controlApiUrl: "http://control.local",
    token: "local-test-jwt",
    idempotencyKey: "123e4567-e89b-42d3-a456-426614174000",
    request,
  });
  assert.equal(created.experiment.experiment_id, EXPERIMENT_ID);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(new Headers(calls[0].options.headers).get("Idempotency-Key"), "123e4567-e89b-42d3-a456-426614174000");
  assert.deepEqual(JSON.parse(calls[0].options.body), request);
  assert.equal(JSON.stringify(calls[0]).includes("tenant_id"), false);

  globalThis.fetch = async () => jsonResponse({
    experiment: activeExperimentInfo(),
    idempotent_replay: true,
    request_id: "request_wrong_status",
    trace_id: "trace_wrong_status",
  });
  await assert.rejects(
    () => createExperiment({
      controlApiUrl: "http://control.local",
      token: "local-test-jwt",
      idempotencyKey: "123e4567-e89b-42d3-a456-426614174000",
      request,
    }),
    (error) => error.code === "experiment.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse({
    experiment: activeExperimentInfo({ name: "Different immutable experiment" }),
    idempotent_replay: false,
    request_id: "request_wrong_owner",
    trace_id: "trace_wrong_owner",
  }, { status: 201 });
  await assert.rejects(
    () => createExperiment({
      controlApiUrl: "http://control.local",
      token: "local-test-jwt",
      idempotencyKey: "123e4567-e89b-42d3-a456-426614174000",
      request,
    }),
    (error) => error.code === "experiment.response_invalid",
  );
});

test("renderer contains authoritative A/B rows without internal fingerprints", () => {
  const context = authContext();
  const shell = shellState(context);
  const html = renderExperiments({
    state: "ready",
    experiments: [experimentInfo()],
    selectedExperimentId: EXPERIMENT_ID,
    detailState: "ready",
    experiment: experimentInfo(),
    resultsState: "ready",
    results: resultsPayload(),
    canCreate: true,
    createOpen: false,
    createState: "idle",
    createOptionsState: "ready",
    createOptions: { projects: [], environments: [], modelAliases: [], models: [] },
  }, context, shell);
  assert.match(html, /A\/B 结果/);
  assert.match(html, /模型 A/);
  assert.match(html, /模型 B/);
  assert.match(html, /A \/ B 目标/);
  assert.match(html, /反馈窗口/);
  assert.match(html, /算法版本/);
  assert.doesNotMatch(html, /experiment_authz_a|route_config_fingerprint|provider_base_url/);

  const noFeedbackHtml = renderExperiments({
    state: "ready",
    experiments: [experimentInfo()],
    selectedExperimentId: EXPERIMENT_ID,
    detailState: "ready",
    experiment: experimentInfo(),
    resultsState: "ready",
    results: resultsPayload({
      results: [
        armResult("a", {
          feedback_count: 0,
          rating_sum: 0,
          average_rating_milli: 0,
          accepted_count: 0,
          adoption_rate_bps: 0,
        }),
        armResult("b", {
          feedback_count: 0,
          rating_sum: 0,
          average_rating_milli: 0,
          accepted_count: 0,
          adoption_rate_bps: 0,
        }),
      ],
    }),
    canCreate: false,
  }, context, shell);
  assert.match(noFeedbackHtml, /<dt>平均评分<\/dt><dd>-<\/dd>/);
  assert.match(noFeedbackHtml, /<dt>采纳率<\/dt><dd>-<\/dd>/);

  const readOnly = authContext([PERMISSIONS.EXPERIMENT_READ]);
  const readOnlyHtml = renderExperiments({
    state: "ready",
    experiments: [],
    selectedExperimentId: "",
    canCreate: false,
  }, readOnly, shellState(readOnly));
  assert.doesNotMatch(readOnlyHtml, /data-experiment-create|data-experiment-dialog/);
});

test("PAGE-105 is wired into the authorized Console shell without role fallback", () => {
  const context = authContext();
  const shell = shellState(context);
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: context,
    consoleShellState: shell,
    experimentsState: {
      state: "ready",
      experiments: [experimentInfo()],
      selectedExperimentId: "",
      canCreate: true,
      createOptionsState: "ready",
      createOptions: createOptionsPayload(),
    },
  });
  assert.match(container.innerHTML, /data-experiments-root/);
  assert.match(container.innerHTML, /模型评测/);

  const noRead = authContext(CREATE_PERMISSIONS.filter(
    (code) => code !== PERMISSIONS.EXPERIMENT_READ,
  ));
  const deniedContainer = { innerHTML: "" };
  renderDeveloperStart(deniedContainer, {
    authContext: noRead,
    consoleShellState: shellState(noRead),
    experimentsState: { state: "ready", experiments: [experimentInfo()] },
  });
  assert.doesNotMatch(deniedContainer.innerHTML, /data-experiments-root/);
});

test("PAGE-105 gates all four create option reads behind the composite owner", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(main, /function shouldLoadExperimentCreateResources/);
  for (const stateName of ["environmentState", "modelsState", "modelAliasesState", "projectState"]) {
    assert.match(
      main,
      new RegExp(`resources\\.${stateName}`),
    );
  }
  // Dialog 只能在重新读取 Session 与后端导航后阻止 DOM 替换，不能让定时器跳过撤权发现。
  assert.match(
    main,
    /activeNavigationItemId !== "experiments"[\s\S]*else if \([\s\S]*isBackgroundRefresh[\s\S]*experimentPageState\.blocksShellDomReplacement\(\)[\s\S]*experimentPageState\.enter\(currentExperimentOwner\(consoleShellState\)\)/,
  );
  assert.match(main, /case "experiments":[\s\S]*shouldLoadExperimentCreateResources\(consoleShellState\)[\s\S]*loadExperimentsState/);
  assert.doesNotMatch(main, /window\.setInterval\(\(\) => \{[\s\S]*?void refresh\(\);/);
  assert.equal(
    main.match(/experimentPageState\.blocksShellDomReplacement\(\)/g)?.length,
    1,
  );
  // 详情点击必须局部投影 loading/结果；全控制台 refresh 会让无关慢接口阻塞可见反馈。
  assert.match(
    main,
    /async onSelect\(experimentId\)[\s\S]*?refreshSelectedExperimentPanel\(consoleShellState\)/,
  );
  assert.match(
    main,
    /async function refreshSelectedExperimentPanel[\s\S]*?const refreshId = \+\+refreshSequence;[\s\S]*?loadExperimentDetailState\(consoleShellState, refreshId\)[\s\S]*?renderCurrentExperimentPanel\(consoleShellState\)/,
  );
  // 只有已完成 Session/RBAC/navigation 复核的后台轮次才能请求状态机保留 final 统计快照。
  assert.match(
    main,
    /loadExperimentsState\(consoleShellState, refreshId,[\s\S]*?preserveFinalOnStatisticsUnavailable: isBackgroundRefresh/,
  );
  assert.match(
    main,
    /loadExperimentDetailState\(consoleShellState, refreshId, \{[\s\S]*?preserveFinalOnStatisticsUnavailable[\s\S]*?experimentPageState\.rejectDetail\([\s\S]*?\{ preserveFinalOnStatisticsUnavailable \}/,
  );
});

test("PAGE-105 CSS provides desktop and mobile layout constraints", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.experiment-row\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.experiment-result-grid/);
  assert.match(css, /max-height: calc\(100vh - 36px\)/);
});
