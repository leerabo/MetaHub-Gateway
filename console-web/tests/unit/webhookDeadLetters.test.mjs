// PRD-057 Console 专项：冻结 PAGE-083 API 白名单、完整身份 owner、持久重放幂等和零泄露渲染合同。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createWebhookDeadLetterPageState,
  isWebhookDeadLetterAbortError,
  webhookDeadLetterIdentitySnapshot,
} from "../../src/features/webhook-dead-letters/webhookDeadLetterPageState.js";
import { renderWebhookDeadLetters } from "../../src/features/webhook-dead-letters/renderWebhookDeadLetters.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  archiveWebhookDeadLetter,
  listWebhookDeadLetters,
  replayWebhookDelivery,
  shouldRetainWebhookDeadLetterIdempotencyKey,
} from "../../src/lib/webhooks.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造只含公开 JSON 的 fetch 响应，避免测试依赖真实网络。 */
function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: { get: () => null },
    async json() {
      return payload;
    },
  };
}

/** 构造包含全部 owner 维度的认证上下文。 */
function authContext({
  tenantId = "tenant_a",
  userId = "usr_a",
  role = "admin",
  sessionId = "session_a",
  sessionRevision = 1,
  permissionCodes = [PERMISSIONS.WEBHOOK_MANAGE],
} = {}) {
  // Webhook 管理权限由测试参数显式控制，角色名仅用于验证身份 owner 漂移。
  return withPermissionSnapshot({
    user: { tenant_id: tenantId, user_id: userId, role },
    tenant: { tenant_id: tenantId, name: "测试租户" },
    session: { session_id: sessionId, session_revision: sessionRevision },
  }, permissionCodes, { permissionRevision: sessionRevision });
}

/** 构造不含 payload、URL、Secret 或 worker 字段的安全死信 DTO。 */
function deadLetterFixture(overrides = {}) {
  return {
    dead_letter_event_id: "whdl_0123456789abcdef0123456789abcdef",
    webhook_delivery_id: "whd_source_1",
    webhook_endpoint_id: "whe_ops_1",
    webhook_endpoint_name: "运维告警",
    event_id: "evt_budget_1",
    event_type: "budget.threshold_reached",
    status: "dead_letter",
    failure_error_code: "webhook.receiver_http_error",
    failure_http_status: 500,
    failed_at: "2026-07-30T02:00:00Z",
    expires_at: "2026-08-29T02:00:00Z",
    current_replay_delivery_id: null,
    replay_requested_at: null,
    replay_dispatch_attempts: 0,
    last_replay_error_code: null,
    replayed_at: null,
    archived_at: null,
    latest_attempt: {
      attempt_no: 3,
      status: "failed",
      http_status: 500,
      error_code: "webhook.receiver_http_error",
      error_summary: "receiver-body-must-not-render",
      created_at: "2026-07-30T02:00:00Z",
    },
    replayable: true,
    allowed_actions: ["replay", "archive"],
    ...overrides,
  };
}

/** 构造与后端 Pydantic 顶层合同一致的冻结列表响应，防止测试夹具漂移。 */
function listPayload(events = [deadLetterFixture()], overrides = {}) {
  return {
    dead_letters: events,
    next_cursor: "opaque.signed.cursor",
    filters: {
      status: "all",
      event_type: null,
      webhook_endpoint_id: null,
      limit: 50,
    },
    request_id: "req_dead_letter_list",
    trace_id: "trc_dead_letter_list",
    ...overrides,
  };
}

test("PRD-057 frontend response fixtures stay aligned with frozen backend models", async () => {
  const modelsSource = await readFile(
    new URL("../../../control-plane-api/src/metahub_control/webhooks/models.py", import.meta.url),
    "utf8",
  );
  const attemptContract = modelsSource.slice(
    modelsSource.indexOf("class WebhookDeliveryAttemptInfo"),
    modelsSource.indexOf("class WebhookDeliveryInfo"),
  );
  const publicDeadLetterContract = modelsSource.slice(
    modelsSource.indexOf("class WebhookDeadLetterAttemptInfo"),
    modelsSource.indexOf("class WebhookDeadLetterFilters"),
  );
  const listContract = modelsSource.slice(
    modelsSource.indexOf("class WebhookDeadLetterListResponse"),
    modelsSource.indexOf("class WebhookDeliveryReplayResponse"),
  );
  const replayContract = modelsSource.slice(
    modelsSource.indexOf("class WebhookDeliveryReplayResponse"),
    modelsSource.indexOf("class WebhookDeadLetterArchiveResponse"),
  );
  const archiveContract = modelsSource.slice(
    modelsSource.indexOf("class WebhookDeadLetterArchiveResponse"),
    modelsSource.indexOf("class WebhookSecretRotateResponse"),
  );

  assert.match(attemptContract, /attempt_no: int/);
  assert.match(attemptContract, /created_at: datetime/);
  assert.doesNotMatch(attemptContract, /attempt_number|attempted_at/);
  assert.match(publicDeadLetterContract, /latest_attempt: WebhookDeadLetterAttemptInfo/);
  assert.match(publicDeadLetterContract, /current_replay_delivery: WebhookDeadLetterDeliveryInfo/);
  assert.doesNotMatch(
    publicDeadLetterContract,
    /^\s+(?:tenant_id|replay_requested_by|archived_by|replayed_by|secret_version|error_summary):/m,
  );
  assert.match(listContract, /dead_letters: list\[WebhookDeadLetterInfo\]/);
  assert.match(listContract, /next_cursor: str \| None/);
  assert.match(listContract, /filters: WebhookDeadLetterFilters/);
  assert.doesNotMatch(listContract, /dead_letter_events|event_types|webhook_endpoints|has_more/);
  assert.match(replayContract, /delivery: WebhookDeadLetterDeliveryInfo/);
  assert.match(replayContract, /dead_letter: WebhookDeadLetterInfo/);
  assert.match(archiveContract, /dead_letter: WebhookDeadLetterInfo/);
});

test("PRD-057 clients use exact top-level paths and never send tenant payload or actor", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    const pathname = new URL(String(url)).pathname;
    if (pathname.endsWith("/replay")) {
      return jsonResponse({
        delivery: { webhook_delivery_id: "whd_replay_1" },
        dead_letter: deadLetterFixture({ status: "replayed", allowed_actions: ["archive"] }),
        replayed: true,
        request_id: "req_dead_letter_replay",
        trace_id: "trc_dead_letter_replay",
      });
    }
    if (pathname.endsWith("/archive")) {
      return jsonResponse({
        dead_letter: deadLetterFixture({ status: "archived", allowed_actions: [] }),
        request_id: "req_dead_letter_archive",
        trace_id: "trc_dead_letter_archive",
      });
    }
    return jsonResponse(listPayload());
  };
  const controller = new AbortController();

  const listResult = await listWebhookDeadLetters({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    status: "dead_letter",
    eventType: "budget.threshold_reached",
    webhookEndpointId: "whe_ops_1",
    limit: 50,
    cursor: "opaque.cursor",
    signal: controller.signal,
    operationRequestId: "req_console_whdl_list",
    operationTraceId: "trc_console_whdl_list",
    tenantId: "tenant_injected",
    payload: { prompt: "must-not-send" },
  });
  const replayResult = await replayWebhookDelivery({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    webhookDeliveryId: "whd_source_1",
    idempotencyKey: "console_1234567890abcdef",
    signal: controller.signal,
    operationRequestId: "req_console_whdl_replay",
    operationTraceId: "trc_console_whdl_replay",
    tenantId: "tenant_injected",
    eventId: "evt_injected",
    payload: { response: "must-not-send" },
  });
  const archiveResult = await archiveWebhookDeadLetter({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    deadLetterEventId: "whdl_0123456789abcdef0123456789abcdef",
    signal: controller.signal,
    operationRequestId: "req_console_whdl_archive",
    operationTraceId: "trc_console_whdl_archive",
    actorUserId: "usr_injected",
  });

  const listUrl = new URL(calls[0].url);
  assert.equal(listUrl.pathname, "/webhook-dead-letters");
  assert.equal(listUrl.searchParams.get("status"), "dead_letter");
  assert.equal(listUrl.searchParams.get("event_type"), "budget.threshold_reached");
  assert.equal(listUrl.searchParams.get("webhook_endpoint_id"), "whe_ops_1");
  assert.equal(listUrl.searchParams.get("cursor"), "opaque.cursor");
  assert.equal(listUrl.searchParams.has("tenant_id"), false);
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.signal, controller.signal);
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_console_whdl_list");
  assert.equal(calls[0].options.headers["X-Trace-ID"], "trc_console_whdl_list");

  assert.equal(calls[1].url, "http://control.local/webhook-deliveries/whd_source_1/replay");
  assert.equal(calls[1].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    idempotency_key: "console_1234567890abcdef",
  });
  assert.doesNotMatch(calls[1].options.body, /tenant|event_id|payload|prompt|response/);
  assert.equal(
    calls[2].url,
    "http://control.local/webhook-dead-letters/whdl_0123456789abcdef0123456789abcdef/archive",
  );
  assert.equal(calls[2].options.method, "POST");
  assert.equal(calls[2].options.body, undefined);
  assert.equal(listResult.dead_letters[0].dead_letter_event_id, "whdl_0123456789abcdef0123456789abcdef");
  assert.equal(replayResult.dead_letter.status, "replayed");
  assert.equal(archiveResult.dead_letter.status, "archived");
});

test("PRD-057 client fails closed on invalid filters and preserves only uncertain replay keys", async (t) => {
  let fetchCount = 0;
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse({});
  };

  await assert.rejects(
    listWebhookDeadLetters({
      controlApiUrl: "http://control.local",
      token: "jwt",
      status: "unknown",
    }),
    (error) => error instanceof AuthApiError && error.code === "webhook.dead_letter_filter_invalid",
  );
  await assert.rejects(
    replayWebhookDelivery({
      controlApiUrl: "http://control.local",
      token: "jwt",
      webhookDeliveryId: "whd_1",
      idempotencyKey: "weak",
    }),
    (error) => error instanceof AuthApiError && error.code === "webhook.idempotency_key_invalid",
  );
  assert.equal(fetchCount, 0);
  assert.equal(shouldRetainWebhookDeadLetterIdempotencyKey(new TypeError("network")), true);
  assert.equal(shouldRetainWebhookDeadLetterIdempotencyKey(new AuthApiError({ status: 503 })), true);
  assert.equal(shouldRetainWebhookDeadLetterIdempotencyKey(new AuthApiError({ status: 429 })), true);
  assert.equal(shouldRetainWebhookDeadLetterIdempotencyKey(new AuthApiError({ status: 409 })), false);
});

test("PRD-057 state strips unknown sensitive fields and hides cursor idempotency and owners", () => {
  const state = createWebhookDeadLetterPageState();
  state.acceptPage(listPayload([{
    ...deadLetterFixture(),
    payload: { prompt: "dynamic-sensitive-prompt" },
    endpoint_url: "https://secret.example/hook",
    replay_claim_token: "dynamic-claim-token",
    replay_lease_owner_id: "dynamic-worker-owner",
    tenant_id: "tenant_should_not_enter_snapshot",
  }], {
    filters: {
      status: "dead_letter",
      event_type: " budget.threshold_reached ",
      webhook_endpoint_id: "whe_ops_1",
      limit: 25,
    },
  }));
  const firstIntent = state.prepareReplay(
    "whdl_0123456789abcdef0123456789abcdef",
    () => "console_dynamic_idempotency_secret",
  );
  const repeatedIntent = state.prepareReplay(
    "whdl_0123456789abcdef0123456789abcdef",
    () => "console_must_not_replace",
  );
  assert.equal(firstIntent.idempotencyKey, "console_dynamic_idempotency_secret");
  assert.equal(repeatedIntent.idempotencyKey, firstIntent.idempotencyKey);

  const snapshotText = JSON.stringify(state.snapshot());
  for (const marker of [
    "opaque.signed.cursor",
    "dynamic-sensitive-prompt",
    "secret.example",
    "dynamic-claim-token",
    "dynamic-worker-owner",
    "receiver-body-must-not-render",
    "tenant_should_not_enter_snapshot",
    "console_dynamic_idempotency_secret",
  ]) {
    assert.equal(snapshotText.includes(marker), false, marker);
  }
  assert.equal(state.snapshot().dead_letter_events[0].event_id, "evt_budget_1");
  assert.equal(state.snapshot().dead_letter_events[0].attempt_count, 3);
  assert.deepEqual(state.snapshot().dead_letter_events[0].latest_attempt, {
    attempt_no: 3,
    status: "failed",
    http_status: 500,
    error_code: "webhook.receiver_http_error",
    created_at: "2026-07-30T02:00:00Z",
  });
  assert.deepEqual(state.snapshot().event_types, ["budget.threshold_reached"]);
  assert.deepEqual(state.snapshot().webhook_endpoints, [
    { webhook_endpoint_id: "whe_ops_1", name: "运维告警" },
  ]);
  assert.deepEqual(state.snapshot().filters, {
    status: "dead_letter",
    eventType: "budget.threshold_reached",
    webhookEndpointId: "whe_ops_1",
    limit: 25,
  });
  state.acceptAction({
    dead_letter: deadLetterFixture({ status: "archived", replayable: false, allowed_actions: [] }),
    request_id: "req_dead_letter_archive",
    trace_id: "trc_dead_letter_archive",
  }, "archive");
  assert.equal(state.snapshot().dead_letter_events[0].status, "archived");
  assert.equal(state.snapshot().request_id, "req_dead_letter_archive");
  assert.equal(state.nextPage(), true);
  assert.equal(state.query().cursor, "opaque.signed.cursor");
  assert.equal(JSON.stringify(state.snapshot()).includes("opaque.signed.cursor"), false);
  state.clear();
  assert.deepEqual(state.query(), { status: "all", eventType: "", webhookEndpointId: "", limit: 50, cursor: null });
});

test("PRD-057 owners reject stale identity navigation generation and abort on clear", () => {
  const state = createWebhookDeadLetterPageState();
  state.acceptPage(listPayload());
  const identity = webhookDeadLetterIdentitySnapshot(authContext());
  const listOwner = { identity, navigation: "webhook-dead-letters", refreshGeneration: 7 };
  const firstList = state.beginListOperation(listOwner);
  const secondList = state.beginListOperation(listOwner);
  assert.equal(firstList.controller.signal.aborted, true);
  assert.equal(state.isListOperationCurrent(firstList, listOwner), false);
  assert.equal(state.isListOperationCurrent(secondList, listOwner), true);
  assert.equal(state.isListOperationCurrent(secondList, { ...listOwner, refreshGeneration: 8 }), false);

  const actionOwner = { identity, navigation: "webhook-dead-letters" };
  const action = state.beginActionOperation(actionOwner, {
    action: "archive",
    deadLetterEventId: "whdl_0123456789abcdef0123456789abcdef",
  });
  assert.equal(state.isActionOperationCurrent(action, actionOwner), true);
  assert.equal(state.isActionOperationCurrent(action, {
    identity: webhookDeadLetterIdentitySnapshot(authContext({ tenantId: "tenant_b" })),
    navigation: "webhook-dead-letters",
  }), false);
  assert.equal(state.isActionOperationCurrent(action, { identity, navigation: "overview" }), false);
  state.clear();
  assert.equal(secondList.controller.signal.aborted, true);
  assert.equal(action.controller.signal.aborted, true);
  assert.equal(state.isActionBusy(), false);
});

test("PRD-057 identity includes tenant user role session and revision for both manager roles", () => {
  const admin = webhookDeadLetterIdentitySnapshot(authContext());
  const developer = webhookDeadLetterIdentitySnapshot(authContext({ role: "developer" }));
  assert.notEqual(admin, "");
  assert.notEqual(developer, "");
  assert.equal(webhookDeadLetterIdentitySnapshot(authContext({ role: "viewer", permissionCodes: [] })), "");
  assert.equal(webhookDeadLetterIdentitySnapshot(authContext({ sessionId: "" })), "");
  assert.notEqual(admin, webhookDeadLetterIdentitySnapshot(authContext({ tenantId: "tenant_b" })));
  assert.notEqual(admin, webhookDeadLetterIdentitySnapshot(authContext({ userId: "usr_b" })));
  assert.notEqual(admin, developer);
  assert.notEqual(admin, webhookDeadLetterIdentitySnapshot(authContext({ sessionId: "session_b" })));
  assert.notEqual(admin, webhookDeadLetterIdentitySnapshot(authContext({ sessionRevision: 2 })));
});

test("PRD-057 pending polling is single-flight bounded and clear cancels the timer", () => {
  const timers = [];
  const cleared = [];
  const state = createWebhookDeadLetterPageState({
    setTimer(callback, delayMs) {
      const timer = { callback, delayMs };
      timers.push(timer);
      return timer;
    },
    clearTimer(timer) {
      cleared.push(timer);
    },
  });
  state.acceptPage(listPayload([
    deadLetterFixture({ status: "replay_pending", allowed_actions: [] }),
  ], { next_cursor: null }));
  let pollCount = 0;
  /** 模拟有界轮询回调，只累计当前页面仍获授权的触发次数。 */
  const callback = () => {
    pollCount += 1;
  };
  assert.equal(state.schedulePoll(callback, { delayMs: 25, maxAttempts: 2 }), true);
  assert.equal(state.schedulePoll(callback, { delayMs: 25, maxAttempts: 2 }), false);
  assert.equal(timers[0].delayMs, 25);
  timers[0].callback();
  assert.equal(pollCount, 1);
  assert.equal(state.schedulePoll(callback, { delayMs: 25, maxAttempts: 2 }), true);
  timers[1].callback();
  assert.equal(pollCount, 2);
  assert.equal(state.schedulePoll(callback, { delayMs: 25, maxAttempts: 2 }), false);

  state.cancelPoll();
  assert.equal(state.schedulePoll(callback, { delayMs: 25, maxAttempts: 2 }), true);
  state.clear();
  assert.equal(cleared.includes(timers[2]), true);
  timers[2].callback();
  assert.equal(pollCount, 2);
});

test("PRD-057 renderer is manager-only escapes evidence and never renders sensitive extras", () => {
  const state = createWebhookDeadLetterPageState();
  state.acceptPage(listPayload([{
    ...deadLetterFixture({
      webhook_endpoint_name: "运维 <script>alert(1)</script>",
      event_id: "evt_<img src=x onerror=alert(1)>",
    }),
    payload: { prompt: "dynamic-sensitive-prompt" },
    endpoint_url: "https://secret.example/hook",
  }]));
  const snapshot = state.snapshot();
  const adminHtml = renderWebhookDeadLetters(snapshot, authContext());
  const developerHtml = renderWebhookDeadLetters(snapshot, authContext({ role: "developer" }));

  assert.match(adminHtml, /Webhook 死信/);
  assert.match(adminHtml, /webhook-dead-letter-filter-form/);
  assert.match(adminHtml, /data-webhook-dead-letter-replay/);
  assert.match(adminHtml, /data-webhook-dead-letter-archive/);
  assert.match(adminHtml, /运维 &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(adminHtml, /<script>|<img src=x/);
  assert.doesNotMatch(adminHtml, /dynamic-sensitive-prompt|secret\.example|payload|tenant_id|lease|claim/);
  assert.match(developerHtml, /Webhook 死信/);
  assert.equal(renderWebhookDeadLetters(
    snapshot,
    authContext({ role: "viewer", permissionCodes: [] }),
  ), "");
});

test("PRD-057 developer shell renders the dedicated navigation and viewer direct render stays empty", () => {
  const state = createWebhookDeadLetterPageState();
  state.acceptPage(listPayload());
  const container = { innerHTML: "" };
  const developer = authContext({ role: "developer" });
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: developer,
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "webhook-dead-letters",
        navigation_items: [{
          id: "webhook-dead-letters",
          label: "Webhook 死信",
          group: "安全与治理",
          target_section: "webhook-dead-letters",
          order: 92,
        }],
      },
      activeNavigationItemId: "webhook-dead-letters",
    },
    webhookDeadLettersState: state.snapshot(),
  });
  assert.match(container.innerHTML, /webhook-dead-letters-panel/);
  assert.match(container.innerHTML, /evt_budget_1/);

  const viewerContainer = { innerHTML: "" };
  renderDeveloperStart(viewerContainer, {
    authContext: authContext({ role: "viewer", permissionCodes: [] }),
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "webhook-dead-letters",
        navigation_items: [{
          id: "webhook-dead-letters",
          label: "Webhook 死信",
          target_section: "webhook-dead-letters",
        }],
      },
    },
    webhookDeadLettersState: state.snapshot(),
  });
  assert.doesNotMatch(viewerContainer.innerHTML, /webhook-dead-letter-page|evt_budget_1/);
});

test("PRD-057 app integration freezes zero-prefetch cleanup polling and responsive contracts", async () => {
  const [mainSource, renderSource, bindSource, css, navigationSource] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/webhook-dead-letters/bindWebhookDeadLetters.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/console/service.py", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /loadWebhookDeadLettersState\(consoleShellState, refreshId\)/);
  assert.match(mainSource, /currentWebhookDeadLetterActionOwner[\s\S]*target_section === "webhook-dead-letters"/);
  assert.match(mainSource, /webhookDeadLetterIdentitySnapshot\(authSession\.context\)/);
  assert.match(mainSource, /function clearConsoleSessionState\([\s\S]*?webhookDeadLetterPageState\.clear\(\)/);
  assert.match(mainSource, /window\.addEventListener\("pagehide"[\s\S]*webhookDeadLetterPageState\.clear\(\)/);
  assert.match(mainSource, /itemId !== "webhook-dead-letters"[\s\S]*webhookDeadLetterPageState\.clear\(\)/);
  assert.match(mainSource, /schedulePoll[\s\S]*delayMs: 1500, maxAttempts: 8/);
  assert.match(mainSource, /refreshWebhookDeadLetterPanel[\s\S]*renderWebhookDeadLetterPanel/);
  assert.match(renderSource, /case "webhook-dead-letters"/);
  assert.match(renderSource, /renderWebhookDeadLettersSection\(options\.webhookDeadLettersState/);
  assert.doesNotMatch(bindSource, /name=["'](?:cursor|tenant_id|idempotency_key)/i);
  assert.doesNotMatch(bindSource, /dataset\.(?:cursor|tenantId|idempotencyKey)/);
  assert.match(bindSource, /dialog\.showModal\(\)/);
  assert.match(css, /\.webhook-dead-letter-table/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.webhook-dead-letter-row/);
  assert.match(css, /overflow-x:\s*visible/);
  assert.match(css, /\.webhook-dead-letter-dialog\s*\{[\s\S]*position:\s*fixed/);
  const navIndex = navigationSource.indexOf('NavigationDefinition(\r\n        "webhook-dead-letters"');
  const portableNavIndex = navIndex === -1
    ? navigationSource.indexOf('NavigationDefinition(\n        "webhook-dead-letters"')
    : navIndex;
  assert.notEqual(portableNavIndex, -1);
  assert.match(navigationSource.slice(portableNavIndex, portableNavIndex + 650), /\("webhook\.manage",\)/);
  assert.equal((css.match(/{/g) ?? []).length, (css.match(/}/g) ?? []).length);
});

test("PRD-057 abort classification suppresses only explicit lifecycle cancellation", () => {
  assert.equal(isWebhookDeadLetterAbortError({ name: "AbortError" }), true);
  assert.equal(isWebhookDeadLetterAbortError(new TypeError("network")), false);
  assert.equal(isWebhookDeadLetterAbortError(null), false);
});
