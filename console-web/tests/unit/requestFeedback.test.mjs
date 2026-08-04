// 本单测覆盖 PRD-049 Feedback API 白名单、幂等 owner、权限、XSS、报表与主应用生命周期接线。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createRequestFeedbackMutationMemory,
  createRequestFeedbackSummaryMemory,
  renderRequestFeedbackSection,
  renderRequestFeedbackSummaryPanel,
  requestFeedbackIdentitySnapshot,
} from "../../src/features/request-feedback/renderRequestFeedback.js";
import { createI18n } from "../../src/i18n/messages.js";
import {
  createRequestFeedback,
  getRequestFeedbackSummary,
} from "../../src/lib/requestFeedback.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造包含完整租户、用户、权限快照和 Session 代次的控制台身份。 */
function authContext(
  role = "developer",
  tenantId = "tenant_a",
  revision = 1,
  permissionCodes = [PERMISSIONS.FEEDBACK_USE],
) {
  // 反馈资格仅来自 feedback.use 快照，角色字段只用于身份切换和展示断言。
  return withPermissionSnapshot({
    user: { tenant_id: tenantId, user_id: `user_${role}`, role },
    tenant: { tenant_id: tenantId },
    session: { session_id: `session_${tenantId}`, session_revision: revision },
  }, permissionCodes, { permissionRevision: revision });
}

/** 构造 RequestLog 详情中的 PRD-049 权威资格合同。 */
function feedbackDetail(requestId, overrides = {}) {
  return {
    request_log: {
      log: { request_id: requestId },
      feedback: null,
      feedback_eligible: true,
      feedback_ineligible_reason: "",
      ...overrides,
    },
  };
}

/** 构造 mutation 回调必须逐字段核对的实时 owner。 */
function mutationOwner(identity, requestId) {
  return { identity, navigation: "request-logs", sourceRequestId: requestId };
}

/** 构造只含公开字段的不可变反馈响应。 */
function feedbackRecord(requestId, overrides = {}) {
  return {
    request_feedback_id: "rfb_11111111111111111111111111111111",
    request_id: requestId,
    model_id: "model_deepseek",
    rating: 5,
    accepted: true,
    tags: ["accurate", "helpful"],
    actor_type: "session_user",
    status: "recorded",
    created_at: "2026-07-20T08:00:00Z",
    ...overrides,
  };
}

test("feedback client keeps source request id separate from operation id and never sends tenant or actor", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 201,
      headers: new Map(),
      async json() {
        return {
          feedback: feedbackRecord("req_source_call"),
          idempotent_replay: false,
          request_id: "req_feedback_operation",
          trace_id: "trc_feedback_operation",
        };
      },
    };
  };

  const controller = new AbortController();
  const result = await createRequestFeedback({
    controlApiUrl: "http://control.local",
    token: "session-secret",
    requestId: "req_source_call",
    rating: 5,
    accepted: true,
    tags: ["accurate", "helpful"],
    operationRequestId: "req_feedback_operation",
    signal: controller.signal,
  });

  assert.equal(result.feedback.request_id, "req_source_call");
  assert.equal(result.request_id, "req_feedback_operation");
  assert.equal(calls[0].url, "http://control.local/feedback");
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_feedback_operation");
  assert.equal(calls[0].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    request_id: "req_source_call",
    rating: 5,
    accepted: true,
    tags: ["accurate", "helpful"],
  });
  assert.doesNotMatch(calls[0].options.body, /tenant_id|actor|token|provider|prompt|response/i);
});

test("summary client sends only frozen filters and projects integer statistics without sensitive fields", async () => {
  let observed;
  globalThis.fetch = async (url, options = {}) => {
    observed = { url: String(url), options };
    return {
      ok: true,
      status: 200,
      headers: new Map(),
      async json() {
        return {
          scope_type: "tenant",
          from_time: "2026-07-01T00:00:00Z",
          to_time: "2026-07-20T00:00:00Z",
          summary: {
            feedback_count: 12,
            rating_sum: 51,
            average_rating_milli: 4250,
            accepted_count: 9,
            adoption_rate_bps: 7500,
            rating_distribution: { 1: 0, 2: 1, 3: 2, 4: 3, 5: 6 },
            tag_counts: [{ tag: "accurate", count: 8, prompt: "must-not-survive" }],
          },
          models: [{
            model_id: "model_1",
            internal_model_id: "deepseek-chat",
            display_name: "DeepSeek Chat",
            feedback_count: 12,
            average_rating_milli: 4250,
            accepted_count: 9,
            adoption_rate_bps: 7500,
            rating_distribution: { 1: 0, 2: 1, 3: 2, 4: 3, 5: 6 },
            provider_key: "must-not-survive",
          }],
          total: 1,
          limit: 20,
          offset: 0,
          request_id: "req_summary",
          trace_id: "trc_summary",
          tenant_id: "must-not-survive",
        };
      },
    };
  };

  const result = await getRequestFeedbackSummary({
    controlApiUrl: "http://control.local",
    token: "session-secret",
    modelId: "model_1",
    fromTime: "2026-07-01T00:00:00Z",
    toTime: "2026-07-20T00:00:00Z",
    limit: 20,
    offset: 0,
  });

  assert.match(observed.url, /model_id=model_1/);
  assert.match(observed.url, /from_time=2026-07-01T00%3A00%3A00Z/);
  assert.doesNotMatch(observed.url, /tenant|actor|virtual_key/);
  assert.equal(observed.options.cache, "no-store");
  assert.equal(result.summary.average_rating_milli, 4250);
  assert.equal(result.summary.adoption_rate_bps, 7500);
  assert.deepEqual(result.summary.tag_counts, [{ tag: "accurate", count: 8 }]);
  assert.equal(result.models[0].provider_key, undefined);
  assert.equal(result.tenant_id, undefined);
});

test("mutation enforces role owner strict form tags and immutable success", () => {
  const context = authContext("developer");
  const identity = requestFeedbackIdentitySnapshot(context);
  const memory = createRequestFeedbackMutationMemory({ requestIdFactory: () => "req_operation_1" });
  const owner = mutationOwner(identity, "req_source_1");

  assert.equal(memory.syncDetail({ identity, navigation: "request-logs", detail: feedbackDetail("req_source_1") }), true);
  assert.equal(memory.selectRating(true), false);
  assert.equal(memory.selectRating(5), true);
  assert.equal(memory.selectAccepted(null), false);
  assert.equal(memory.selectAccepted(true), true);
  assert.equal(memory.toggleTag("accurate", true), true);
  assert.equal(memory.toggleTag("inaccurate", true), false);
  assert.equal(memory.toggleTag("helpful", true), true);
  assert.equal(memory.toggleTag("relevant", true), true);
  assert.equal(memory.toggleTag("complete", true), true);
  assert.equal(memory.toggleTag("clear", true), true);
  assert.equal(memory.toggleTag("unsafe", true), false);
  assert.equal(memory.toggleTag("relevant", false), true);
  assert.equal(memory.toggleTag("complete", false), true);
  assert.equal(memory.toggleTag("clear", false), true);

  const acquired = memory.acquireSubmit(owner);
  assert.equal(acquired.state, "ok");
  assert.equal(acquired.ticket.operationRequestId, "req_operation_1");
  assert.deepEqual(acquired.ticket.tags, ["accurate", "helpful"]);
  assert.equal(memory.acquireSubmit(owner).reused, true);
  assert.equal(memory.acceptSuccess(acquired.ticket, {
    feedback: feedbackRecord("req_source_1"),
    idempotent_replay: false,
  }, owner), true);
  assert.equal(memory.snapshot().authoritativeFeedback.status, "recorded");
  assert.equal(memory.snapshot().statusMessage, "feedback.status.recorded");
  assert.equal(memory.selectRating(4), false);
  assert.equal(memory.cancelDraft(), false);
});

test("network-unknown retry reuses operation id while identity switch aborts and rejects late callbacks", () => {
  const identityA = requestFeedbackIdentitySnapshot(authContext("developer", "tenant_a", 1));
  const identityB = requestFeedbackIdentitySnapshot(authContext("developer", "tenant_b", 2));
  let sequence = 0;
  const memory = createRequestFeedbackMutationMemory({ requestIdFactory: () => `req_operation_${++sequence}` });
  const ownerA = mutationOwner(identityA, "req_shared");
  memory.syncDetail({ identity: identityA, navigation: "request-logs", detail: feedbackDetail("req_shared") });
  memory.selectRating(4);
  memory.selectAccepted(false);
  const first = memory.acquireSubmit(ownerA).ticket;
  assert.equal(memory.acceptFailure(first, { message: "network down" }, ownerA), true);
  const retry = memory.acquireSubmit(ownerA).ticket;
  assert.equal(retry.operationRequestId, first.operationRequestId);

  memory.syncDetail({ identity: identityB, navigation: "request-logs", detail: feedbackDetail("req_shared") });
  assert.equal(retry.signal.aborted, true);
  assert.equal(memory.acceptSuccess(retry, { feedback: feedbackRecord("req_shared") }, ownerA), false);
  assert.equal(memory.snapshot().authoritativeFeedback, null);
  assert.equal(memory.snapshot().dirty, false);
});

test("PAGE-070 renders only admin/developer escapes facts and keeps viewer at zero DOM", () => {
  const state = {
    sourceRequestId: "req_<unsafe>",
    feedbackEligible: true,
    rating: 5,
    accepted: true,
    tags: ["accurate"],
    busy: false,
    dirty: true,
    canSubmit: true,
    actionError: null,
    statusMessage: "",
  };
  const html = renderRequestFeedbackSection(state, authContext("developer"), createI18n("zh-CN"));
  assert.match(html, /data-request-feedback-form/);
  assert.match(html, /req_&lt;unsafe&gt;/);
  assert.match(html, /value="accurate" checked/);
  assert.doesNotMatch(html, /tenant_id|actor_user_id|provider_key|<unsafe>/);
  assert.equal(renderRequestFeedbackSection(
    state,
    authContext("viewer", "tenant_a", 1, []),
    createI18n("zh-CN"),
  ), "");

  const recorded = renderRequestFeedbackSection({
    ...state,
    authoritativeFeedback: feedbackRecord("req_source", { model_id: "<script>model</script>" }),
    feedbackEligible: false,
    statusMessage: "feedback.status.recorded",
  }, authContext("admin"), createI18n("en-US"));
  assert.match(recorded, /Feedback recorded/);
  assert.match(recorded, /&lt;script&gt;model&lt;\/script&gt;/);
  assert.doesNotMatch(recorded, /data-request-feedback-form|<script>model<\/script>|PATCH|DELETE/);
});

test("PAGE-071 memory rejects stale tenant result and renderer uses backend totals with English i18n", () => {
  const memory = createRequestFeedbackSummaryMemory();
  const identityA = requestFeedbackIdentitySnapshot(authContext("developer", "tenant_a"));
  const identityB = requestFeedbackIdentitySnapshot(authContext("developer", "tenant_b"));
  const ownerA = { identity: identityA, navigation: "feedback-summary", query: "query-a", refreshId: 7, currentRefreshId: 7 };
  const ticketA = memory.beginRead(ownerA);
  const ownerB = { identity: identityB, navigation: "feedback-summary", query: "query-b", refreshId: 8, currentRefreshId: 8 };
  const ticketB = memory.beginRead(ownerB);
  assert.equal(ticketA.signal.aborted, true);
  assert.equal(memory.acceptSuccess(ticketA, { scope_type: "tenant", models: [] }, ownerA), false);

  const payload = {
    state: "ok",
    scope_type: "tenant",
    from_time: "2026-07-01T00:00:00Z",
    to_time: "2026-07-20T00:00:00Z",
    summary: {
      feedback_count: 12,
      average_rating_milli: 4250,
      accepted_count: 9,
      adoption_rate_bps: 7500,
      rating_distribution: { 1: 0, 2: 1, 3: 2, 4: 3, 5: 6 },
      tag_counts: [{ tag: "accurate", count: 8 }],
    },
    models: [{
      model_id: "model_<unsafe>",
      internal_model_id: "deepseek-chat",
      display_name: "<script>DeepSeek</script>",
      feedback_count: 12,
      average_rating_milli: 4250,
      accepted_count: 9,
      adoption_rate_bps: 7500,
      rating_distribution: { 1: 0, 2: 1, 3: 2, 4: 3, 5: 6 },
    }],
    total: 1,
    limit: 20,
    offset: 0,
  };
  assert.equal(memory.acceptSuccess(ticketB, payload, ownerB), true);
  const html = renderRequestFeedbackSummaryPanel(memory.snapshot(), authContext("developer", "tenant_b"), {}, createI18n("en-US"));
  assert.match(html, /Feedback report/);
  assert.match(html, /4\.25 \/ 5/);
  assert.match(html, /75%/);
  assert.match(html, /&lt;script&gt;DeepSeek&lt;\/script&gt;/);
  assert.match(html, /deepseek-chat/);
  assert.doesNotMatch(html, /model_&lt;unsafe&gt;/);
  assert.doesNotMatch(html, /<script>DeepSeek<\/script>|tenant_id|provider_key/);
  assert.equal(renderRequestFeedbackSummaryPanel(
    payload,
    authContext("viewer", "tenant_a", 1, []),
    {},
    createI18n("en-US"),
  ), "");
});

test("main wires feedback cleanup permission-gated loading and operation request id", async () => {
  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(mainSource, /pagehide[\s\S]*clearRequestLogSessionState\(\)[\s\S]*clearRequestFeedbackSummarySessionState\(\)/);
  assert.match(mainSource, /function renderLogin[\s\S]*clearRequestLogSessionState\(\)[\s\S]*clearRequestFeedbackSummarySessionState\(\)/);
  // PRD-071 的 PAGE-099 与调用日志共享同一 owner；只有离开这两个受控导航才清理，
  // 页面间切换不得误杀仍由当前租户和权限代次持有的 list/detail 请求。
  assert.match(mainSource, /REQUEST_LOG_NAVIGATION_IDS = new Set\(\["request-logs", "output-safety"\]\)/);
  assert.match(mainSource, /!REQUEST_LOG_NAVIGATION_IDS\.has\([\s\S]*clearRequestLogSessionState\(\)/);
  assert.match(mainSource, /itemId !== "feedback-summary"[\s\S]*clearRequestFeedbackSummarySessionState\(\)/);
  assert.match(mainSource, /hasPermission\(authSession\.context, PERMISSIONS\.FEEDBACK_USE\)[\s\S]*activeNavigationItemId !== "feedback-summary"/);
  assert.match(mainSource, /operationRequestId: ticket\.operationRequestId/);
  assert.doesNotMatch(mainSource, /createRequestFeedback\(\{[\s\S]{0,500}tenantId|createRequestFeedback\(\{[\s\S]{0,500}actor/);
});

test("PAGE-070 and PAGE-071 CSS keep fixed desktop tracks and collapse at 900 and 620 pixels", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.request-feedback-section form\s*\{[\s\S]*grid-template-columns:/);
  assert.match(css, /\.feedback-model-row\s*\{[\s\S]*grid-template-columns:/);
  assert.match(css, /@media \(max-width: 900px\)[\s\S]*\.feedback-model-head\s*\{[\s\S]*display: none/);
  assert.match(css, /@media \(max-width: 620px\)[\s\S]*\.request-feedback-section form,[\s\S]*grid-template-columns: 1fr/);
  assert.match(css, /\.feedback-model-row > \*\s*\{[\s\S]*overflow-wrap: anywhere/);
});
