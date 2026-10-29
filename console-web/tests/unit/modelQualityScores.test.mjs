// 本单测覆盖 PAGE-069 API、双代次、CAS、权限、XSS、无预取和响应式边界。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createModelQualityScorePageMemory,
  modelQualityScoreIdentitySnapshot,
  qualityPercentToBps,
  renderModelQualityScoresPanel,
} from "../../src/features/model-quality-scores/renderModelQualityScoresPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  listModelQualityScores,
  updateModelQualityScore,
} from "../../src/lib/modelQualityScores.js";
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
  session: { session_id: "session_1", session_revision: 4 },
}, [PERMISSIONS.MODEL_QUALITY_READ, PERMISSIONS.MODEL_QUALITY_WRITE]);

/**
 * 构造 GET/PATCH 共用的安全模型评分 DTO。
 * 参数：可覆盖反馈版本、统计版本、人工评分、模型 ID 和展示名；返回值：完整公开 Score 测试对象。
 * 约束：fixture 不包含 Provider Key、Prompt、Response 或其他敏感字段。
 */
function scoreFixture({
  feedbackVersion = 1,
  statisticsVersion = 3,
  manualQualityBps = 7000,
  modelId = "model_1",
  displayName = "DeepSeek Chat",
} = {}) {
  return {
    model_quality_score_id: "mqs_11111111111111111111111111111111",
    tenant_id: "tenant_1",
    model_id: modelId,
    internal_model_id: "deepseek-chat",
    display_name: displayName,
    provider_code: "deepseek",
    provider_name: "DeepSeek",
    model_status: "active",
    status: "active",
    sample_count: statisticsVersion,
    success_count: Math.max(0, statisticsVersion - 1),
    error_count: statisticsVersion ? 1 : 0,
    latency_sample_count: statisticsVersion,
    total_latency_ms: statisticsVersion * 1000,
    success_rate_bps: 6667,
    error_rate_bps: 3333,
    average_latency_ms: "1000.000",
    manual_quality_bps: manualQualityBps,
    effective_quality_bps: 7333,
    observation_status: "observed",
    formula_version: "quality_cost_v1",
    statistics_version: statisticsVersion,
    feedback_version: feedbackVersion,
    // PRD-049 逐请求反馈维度必须与上方 Provider 终态样本和人工反馈版本完全独立。
    request_feedback_count: 12,
    request_feedback_rating_sum: 51,
    request_feedback_average_milli: 4250,
    request_feedback_accepted_count: 9,
    request_feedback_adoption_rate_bps: 7500,
    request_feedback_statistics_version: 12,
    request_feedback_rating_distribution: { 1: 0, 2: 1, 3: 2, 4: 3, 5: 6 },
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T01:00:00Z",
  };
}

/** 建立已完成首轮 GET 的 PAGE-069 内存仓库。 */
function initializedMemory() {
  let sequence = 0;
  const memory = createModelQualityScorePageMemory({
    requestIdFactory: () => `req_quality_${++sequence}`,
  });
  const identity = modelQualityScoreIdentitySnapshot(adminContext);
  const refresh = memory.beginRefresh({
    identity,
    navigation: "model-quality-scores",
    query: JSON.stringify(["", 100, 0]),
  });
  assert.equal(memory.acceptRefresh(refresh, {
    model_quality_scores: [scoreFixture()],
    total: 1,
    limit: 100,
    offset: 0,
  }), true);
  return { memory, identity };
}

test("model quality client sends exact tenant-free GET and PATCH contracts", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_header"]]),
      async json() {
        // GET 与 PATCH 共用 mock，但返回体仍保持各自精确顶层字段。
        return options.method === "PATCH"
          ? {
              model_quality_score: scoreFixture({ feedbackVersion: 2, manualQualityBps: 8355 }),
              changed: true,
              request_id: "req_quality_exact",
            }
          : {
              model_quality_scores: [scoreFixture()],
              total: 1,
              limit: 20,
              offset: 5,
              request_id: "req_list",
            };
      },
    };
  };

  const listed = await listModelQualityScores({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    modelId: "model/一",
    limit: 20,
    offset: 5,
  });
  const controller = new AbortController();
  const updated = await updateModelQualityScore({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    modelId: "model_1",
    manualQualityBps: 8355,
    expectedFeedbackVersion: 1,
    requestId: "req_quality_exact",
    signal: controller.signal,
  });

  assert.equal(listed.model_quality_scores.length, 1);
  assert.match(calls[0].url, /^http:\/\/control\.local\/model-quality-scores\?/);
  assert.match(calls[0].url, /model_id=model%2F%E4%B8%80/);
  assert.match(calls[0].url, /limit=20/);
  assert.match(calls[0].url, /offset=5/);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.match(calls[0].options.headers["X-Request-ID"], /^req_console_model_quality_/);
  assert.equal(updated.model_quality_score.feedback_version, 2);
  assert.equal(calls[1].options.headers["X-Request-ID"], "req_quality_exact");
  assert.equal(calls[1].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    model_id: "model_1",
    manual_quality_bps: 8355,
    expected_feedback_version: 1,
  });
  assert.doesNotMatch(calls[0].url + calls[1].options.body, /tenant_id|actor|rating|accepted|tags|statistics/);
});

test("model quality client exposes only whitelisted 409 rebase score and request id", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "model_quality_score.version_conflict",
        message: "version changed",
        request_id: "req_conflict",
        details: {
          current_model_quality_score: {
            ...scoreFixture({ feedbackVersion: 3, manualQualityBps: 9000 }),
            provider_key: "must-not-survive",
            response: "must-not-survive",
          },
        },
      };
    },
  });

  await assert.rejects(
    () => updateModelQualityScore({
      controlApiUrl: "http://control.local",
      token: "jwt-token",
      modelId: "model_1",
      manualQualityBps: 8000,
      expectedFeedbackVersion: 1,
      requestId: "req_conflict",
    }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 409);
      assert.equal(error.requestId, "req_conflict");
      assert.equal(error.currentModelQualityScore.feedback_version, 3);
      assert.equal("provider_key" in error.currentModelQualityScore, false);
      assert.equal("response" in error.currentModelQualityScore, false);
      return true;
    },
  );
});

test("quality percentages convert to exact bps without float drift", () => {
  assert.equal(qualityPercentToBps("83.55"), 8355);
  assert.equal(qualityPercentToBps("0"), 0);
  assert.equal(qualityPercentToBps("100.00"), 10000);
  assert.equal(qualityPercentToBps("0.001"), null);
  assert.equal(qualityPercentToBps("100.01"), null);
  assert.equal(qualityPercentToBps("1e2"), null);
  assert.equal(qualityPercentToBps("<script>"), null);
});

test("busy PATCH survives GET refresh and validates exact feedback CAS response", () => {
  const { memory, identity } = initializedMemory();
  assert.equal(memory.selectManualQuality("model_1", 8355), true);
  const save = memory.acquireSave({
    identity,
    navigation: "model-quality-scores",
    modelId: "model_1",
  });
  const refresh = memory.beginRefresh({
    identity,
    navigation: "model-quality-scores",
    query: JSON.stringify(["", 100, 0]),
  });
  assert.equal(memory.acceptRefresh(refresh, {
    model_quality_scores: [scoreFixture({ statisticsVersion: 4 })],
    total: 1,
    limit: 100,
    offset: 0,
  }), true);
  assert.equal(memory.snapshot().rows[0].busy, true);
  assert.equal(memory.snapshot().rows[0].baseFeedbackVersion, 1);

  assert.equal(memory.acceptSuccess(save.ticket, {
    changed: true,
    model_quality_score: scoreFixture({
      feedbackVersion: 2,
      statisticsVersion: 5,
      manualQualityBps: 8355,
    }),
  }, { identity, navigation: "model-quality-scores" }), true);
  const row = memory.snapshot().rows[0];
  assert.equal(row.busy, false);
  assert.equal(row.dirty, false);
  assert.equal(row.baseFeedbackVersion, 2);
  assert.equal(row.score.statistics_version, 5);
});

test("network retry reuses request id while 409 requires explicit rebase", () => {
  const first = initializedMemory();
  first.memory.selectManualQuality("model_1", 8200);
  const unknown = first.memory.acquireSave({
    identity: first.identity,
    navigation: "model-quality-scores",
    modelId: "model_1",
  }).ticket;
  assert.equal(first.memory.acceptFailure(unknown, new Error("network"), {
    identity: first.identity,
    navigation: "model-quality-scores",
  }), true);
  const retry = first.memory.acquireSave({
    identity: first.identity,
    navigation: "model-quality-scores",
    modelId: "model_1",
  }).ticket;
  assert.equal(retry.requestId, unknown.requestId);

  const second = initializedMemory();
  second.memory.selectManualQuality("model_1", 8200);
  const conflict = second.memory.acquireSave({
    identity: second.identity,
    navigation: "model-quality-scores",
    modelId: "model_1",
  }).ticket;
  const error = new AuthApiError({ status: 409, requestId: "req_409" });
  error.currentModelQualityScore = scoreFixture({ feedbackVersion: 2, manualQualityBps: 9000 });
  assert.equal(second.memory.acceptFailure(conflict, error, {
    identity: second.identity,
    navigation: "model-quality-scores",
  }), true);
  assert.equal(second.memory.snapshot().rows[0].conflicted, true);
  assert.equal(second.memory.acquireSave({
    identity: second.identity,
    navigation: "model-quality-scores",
    modelId: "model_1",
  }).state, "blocked");
  assert.equal(second.memory.rebase("model_1"), true);
  assert.equal(second.memory.snapshot().rows[0].baseFeedbackVersion, 2);
  assert.equal(second.memory.snapshot().rows[0].dirty, false);
});

test("query and identity invalidation abort GET and PATCH and reject late responses", () => {
  const { memory, identity } = initializedMemory();
  const oldGet = memory.beginRefresh({
    identity,
    navigation: "model-quality-scores",
    query: JSON.stringify(["", 100, 0]),
  });
  const nextGet = memory.beginRefresh({
    identity,
    navigation: "model-quality-scores",
    query: JSON.stringify(["model_1", 100, 0]),
  });
  assert.equal(oldGet.signal.aborted, true);
  assert.equal(memory.acceptRefresh(oldGet, { model_quality_scores: [] }), false);
  assert.equal(memory.acceptRefresh(nextGet, {
    model_quality_scores: [scoreFixture()],
    total: 1,
    limit: 100,
    offset: 0,
  }), true);
  memory.selectManualQuality("model_1", 8100);
  const save = memory.acquireSave({
    identity,
    navigation: "model-quality-scores",
    modelId: "model_1",
  }).ticket;
  memory.invalidate();
  assert.equal(save.signal.aborted, true);
  assert.equal(memory.acceptFailure(save, new AuthApiError({ status: 401 }), {
    identity,
    navigation: "model-quality-scores",
  }), false);
  assert.equal(memory.snapshot().rows.length, 0);
});

test("PAGE-069 renders admin editor developer readonly viewer empty and escapes server text", () => {
  const { memory } = initializedMemory();
  const state = memory.snapshot();
  state.rows[0].score.display_name = "<script>model</script>";
  state.rows[0].score.provider_name = "<img src=x>";
  const developerContext = withPermissionSnapshot(
    { ...adminContext, user: { ...adminContext.user, role: "developer" } },
    [PERMISSIONS.MODEL_QUALITY_READ],
  );
  const viewerContext = withPermissionSnapshot(
    { ...adminContext, user: { ...adminContext.user, role: "viewer" } },
    [PERMISSIONS.CONSOLE_BASIC_READ],
  );

  const adminHtml = renderModelQualityScoresPanel(state, adminContext, { modelId: "<unsafe>" });
  const developerHtml = renderModelQualityScoresPanel(state, developerContext);
  assert.match(adminHtml, /data-model-quality-form/);
  assert.match(adminHtml, /manualQualityPercent/);
  assert.match(adminHtml, /for="model-quality-model-id"/);
  assert.match(adminHtml, /id="model-quality-model-id"/);
  assert.match(adminHtml, /aria-label="人工评分（%）：model_1"/);
  assert.match(adminHtml, /&lt;script&gt;model&lt;\/script&gt;/);
  assert.match(adminHtml, /value="&lt;unsafe&gt;"/);
  assert.match(adminHtml, /逐请求反馈/);
  assert.match(adminHtml, /4\.25 \/ 5/);
  assert.match(adminHtml, /12 条 \/ 已采纳 9 \/ 75%/);
  assert.match(adminHtml, /分布 1:0 \/ 2:1 \/ 3:2 \/ 4:3 \/ 5:6 \/ v12/);
  assert.match(adminHtml, /反馈 v1/);
  assert.match(adminHtml, /统计 v3/);
  assert.doesNotMatch(adminHtml, /<script>model<\/script>|<img src=x>/);
  assert.match(developerHtml, /模型评分/);
  assert.doesNotMatch(developerHtml, /data-model-quality-form|manualQualityPercent|data-model-quality-clear|data-model-quality-rebase/);
  assert.equal(renderModelQualityScoresPanel(state, viewerContext), "");
});

test("developer start dispatches PAGE-069 only for authorized active navigation", () => {
  const { memory } = initializedMemory();
  const options = {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: adminContext,
    modelQualityScoresState: memory.snapshot(),
    modelQualityScoreFilters: { modelId: "", limit: 100, offset: 0 },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "model-quality-scores",
        navigation_items: [{
          id: "model-quality-scores",
          label: "模型评分",
          group: "配置",
          target_section: "model-quality-scores",
          description: "Quality",
          order: 66,
        }],
      },
    },
  };
  const adminContainer = { innerHTML: "" };
  renderDeveloperStart(adminContainer, options);
  assert.match(adminContainer.innerHTML, /model-quality-scores-panel/);
  assert.match(adminContainer.innerHTML, /data-model-quality-form/);

  const viewerContainer = { innerHTML: "" };
  renderDeveloperStart(viewerContainer, {
    ...options,
    authContext: withPermissionSnapshot(
      { ...adminContext, user: { ...adminContext.user, role: "viewer" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
  });
  assert.doesNotMatch(viewerContainer.innerHTML, /model-quality-scores-panel|model-quality-filter-form/);
});

test("main loader gates PAGE-069 prefetch and lifecycle cleanup by permission active navigation and owner", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  // 以紧随函数的 JSDoc 为切片终点，避免后续页面加载器的租户字段污染 PAGE-069 边界断言。
  const loaderStart = source.indexOf("async function loadModelQualityScoresState");
  const loader = source.slice(
    loaderStart,
    source.indexOf("/**", loaderStart),
  );
  assert.match(loader, /!hasPermission\(authSession\.context, PERMISSIONS\.MODEL_QUALITY_READ\)/);
  assert.match(loader, /activeNavigationItemId !== "model-quality-scores"/);
  assert.match(loader, /refreshId !== refreshSequence/);
  assert.match(loader, /currentEvaluationPageIdentity/);
  assert.match(loader, /listModelQualityScores/);
  assert.doesNotMatch(loader, /tenantId|tenant_id/);
  assert.match(source, /pagehide[\s\S]*clearModelQualityScoreSessionState\(\)/);
  assert.match(source, /itemId !== "model-quality-scores"[\s\S]*clearModelQualityScoreSessionState\(\)/);
});

test("PAGE-069 CSS defines stable desktop 900 and 390 layouts without horizontal tracks", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.model-quality-row\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /@media \(max-width: 900px\)[\s\S]*\.model-quality-row:not\(\.model-quality-head\)/);
  assert.match(css, /@media \(max-width: 390px\)[\s\S]*\.model-quality-row:not\(\.model-quality-head\)/);
  assert.match(css, /\.model-quality-scores-panel\s*\{[^}]*overflow: hidden/s);
  assert.match(css, /\.request-routing-quality\s*\{[^}]*grid-column: 1 \/ -1/s);
  assert.match(css, /overflow-wrap: anywhere/);
});
