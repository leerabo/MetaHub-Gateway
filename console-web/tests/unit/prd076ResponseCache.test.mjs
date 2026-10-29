/**
 * PRD-076 PAGE-106 与 RequestLog Response Cache 投影专项测试。
 * 固定样本只包含合成 Alias、策略、状态、计数与金额，不含 Token、Prompt、Response、Key 或摘要。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  responseCacheInvalidationRequestFromFormData,
  responseCachePolicyRequestFromFormData,
} from "../../src/features/cache-policies/bindResponseCache.js";
import {
  createResponseCachePageState,
  responseCachePageOwner,
} from "../../src/features/cache-policies/responseCachePageState.js";
import { renderResponseCache } from "../../src/features/cache-policies/renderResponseCache.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderResponseCacheEvidence } from "../../src/features/request-logs/renderRequestLogPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  invalidateResponseCache,
  listResponseCachePolicies,
  normalizeResponseCachePolicyList,
  normalizeResponseCacheRequestLogInfo,
  responseCacheSelectors,
  updateResponseCachePolicy,
} from "../../src/lib/responseCache.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
const TENANT_ID = "tenant_cache_a";
const ALIAS_ID = "mla_cache_a";
const POLICY_ID = `rcp_${"a".repeat(32)}`;
const INVALIDATION_ID = `rci_${"b".repeat(32)}`;
const READ = [PERMISSIONS.RESPONSE_CACHE_READ];
const ALL = [
  PERMISSIONS.RESPONSE_CACHE_READ,
  PERMISSIONS.RESPONSE_CACHE_WRITE,
  PERMISSIONS.RESPONSE_CACHE_INVALIDATE,
];

/** 构造 tenant/Session/RBAC 镜像完整的 PAGE-106 当前身份。 */
/** 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。 */
function authContext(permissions = ALL, fingerprint = "response_cache_authz_a") {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, slug: "cache", display_name: "Cache tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: "user_cache_a",
      email: "cache@example.test",
      role: "finance_admin",
    },
    session: { session_id: "session_cache_a", session_revision: 1 },
  }, permissions, { permissionRevision: 12, authorizationFingerprint: fingerprint });
}

/** 构造只由 response_cache.read 控制的后端导航快照。 */
function shellState(context = authContext()) {
  return {
    state: "ok",
    activeNavigationItemId: "response-cache",
    navigation: {
      active_navigation_item_id: "response-cache",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "response-cache",
        target_section: "response-cache",
        label: "响应缓存",
        group: "调用与成本",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.RESPONSE_CACHE_READ],
      }],
    },
  };
}

/** 构造 GET/PATCH 共用的严格策略摘要。 */
function policy(overrides = {}) {
  return {
    cache_policy_id: POLICY_ID,
    tenant_id: TENANT_ID,
    model_alias_id: ALIAS_ID,
    status: "enabled",
    data_class: "internal",
    single_data_class_confirmed: true,
    ttl_seconds: 900,
    charge_cache_hits: false,
    tags: ["finance"],
    version: 1,
    ready_entry_count: 3,
    updated_at: "2026-08-10T08:00:00Z",
    ...overrides,
  };
}

/** 构造分页、排序、tenant owner 均一致的策略列表。 */
function listPayload(overrides = {}) {
  return {
    tenant_id: TENANT_ID,
    items: [{
      tenant_id: TENANT_ID,
      model_alias_id: ALIAS_ID,
      alias: "chat-default",
      environment_id: "environment_cache_a",
      environment_code: "prod",
      cache_policy: policy(),
    }],
    limit: 50,
    next_cursor: null,
    has_more: false,
    global_enabled: true,
    request_id: "request_cache_list_a",
    trace_id: "trace_cache_list_a",
    ...overrides,
  };
}

/** 为 API 客户端构造 exact no-store/private 和链路镜像响应。 */
function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
      ...headers,
    },
  });
}

/** 完整 PATCH 请求基线用于状态机 dirty/CAS/幂等测试。 */
function policyRequest(overrides = {}) {
  return {
    model_alias_id: ALIAS_ID,
    status: "enabled",
    data_class: "internal",
    single_data_class_confirmed: true,
    ttl_seconds: 900,
    charge_cache_hits: false,
    tags: ["finance"],
    expected_version: 1,
    ...overrides,
  };
}

/** hit DTO 的四组金额满足微元 saved=current-reference-billed 等式。 */
/** 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。 */
function hitProjection(overrides = {}) {
  return {
    tenant_id: TENANT_ID,
    status: "hit",
    hit: true,
    charge_cache_hits: false,
    cache_policy_id: POLICY_ID,
    cache_policy_version: 1,
    source_generation_cost_status: "known",
    source_generation_cost_cny: "0.010000",
    current_reference_cost_status: "known",
    current_reference_cost_cny: "0.012300",
    saved_cost_status: "known",
    saved_cost_cny: "0.012300",
    billed_cost_status: "known",
    billed_cost_cny: "0.000000",
    ...overrides,
  };
}

test("response cache client enforces exact tenant DTO, no-store mirror, bearer, and no cookies", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(listPayload());
  };
  const result = await listResponseCachePolicies({
    controlApiUrl: "http://control.local",
    token: "fixture-session-token",
    tenantId: TENANT_ID,
    limit: 50,
    requestId: "console_cache_list_a",
  });
  assert.equal(result.items[0].cache_policy.ready_entry_count, 3);
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.headers.Authorization, "Bearer fixture-session-token");
  assert.equal(calls[0].options.headers["X-Request-ID"], "console_cache_list_a");
  assert.deepEqual([...new URL(calls[0].url).searchParams.keys()], ["limit"]);

  assert.equal(normalizeResponseCachePolicyList({
    ...listPayload(),
    items: [{ ...listPayload().items[0], tenant_id: "tenant_cache_b" }],
  }, TENANT_ID, 50), null);
  assert.equal(normalizeResponseCachePolicyList({
    ...listPayload(),
    cache_key: "forbidden-extra",
  }, TENANT_ID, 50), null);
});

test("response cache client rejects header/body drift and malformed success without rendering data", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async () => jsonResponse(listPayload(), 200, {
    "X-Request-ID": "different_request_id",
  });
  await assert.rejects(
    listResponseCachePolicies({
      controlApiUrl: "http://control.local",
      token: "fixture-token",
      tenantId: TENANT_ID,
      requestId: "console_cache_list_b",
    }),
    (error) => error instanceof AuthApiError && error.code === "response_cache.response_invalid",
  );
});

test("PATCH and invalidate send exact bodies with stable idempotency and fresh attempt request ids", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith("/cache-policies")) {
      return jsonResponse({
        tenant_id: TENANT_ID,
        cache_policy: policy({ version: 2, ttl_seconds: 1200, tags: ["finance", "shared"] }),
        changed: true,
        idempotent_replay: false,
        invalidated_entry_count: 3,
        fenced_fill_count: 1,
        request_id: "request_cache_patch_a",
        trace_id: "trace_cache_patch_a",
      });
    }
    return jsonResponse({
      tenant_id: TENANT_ID,
      invalidation: {
        cache_invalidation_id: INVALIDATION_ID,
        tenant_id: TENANT_ID,
        scope: "tags",
        match_mode: "all",
        tag_count: 1,
        matched_entry_count: 3,
        deleted_entry_count: 3,
        fenced_fill_count: 1,
        reason_code: "manual_finops",
        created_at: "2026-08-10T08:01:00Z",
      },
      idempotent_replay: false,
      request_id: "request_cache_invalidate_a",
      trace_id: "trace_cache_invalidate_a",
    });
  };
  const patchRequest = policyRequest({
    ttl_seconds: 1200,
    tags: ["finance", "shared"],
  });
  await updateResponseCachePolicy({
    controlApiUrl: "http://control.local",
    token: "fixture-token",
    tenantId: TENANT_ID,
    request: patchRequest,
    idempotencyKey: "response-cache-patch-fixture-0001",
    requestId: "console_cache_patch_attempt_1",
  });
  await invalidateResponseCache({
    controlApiUrl: "http://control.local",
    token: "fixture-token",
    tenantId: TENANT_ID,
    request: {
      scope: "tags",
      match_mode: "all",
      tags: [`model_alias:${ALIAS_ID}`],
      reason_code: "manual_finops",
    },
    idempotencyKey: "response-cache-invalidate-fixture-01",
    requestId: "console_cache_invalidate_attempt_1",
  });
  assert.deepEqual(JSON.parse(calls[0].options.body), patchRequest);
  assert.equal(calls[0].options.headers["Idempotency-Key"], "response-cache-patch-fixture-0001");
  assert.equal(calls[0].options.headers["X-Request-ID"], "console_cache_patch_attempt_1");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    scope: "tags",
    match_mode: "all",
    tags: [`model_alias:${ALIAS_ID}`],
    reason_code: "manual_finops",
  });
  assert.equal(calls[1].options.headers["X-Request-ID"], "console_cache_invalidate_attempt_1");
});

test("form projection requires explicit data-class confirmation and all-match selector shape", () => {
  const policyForm = new FormData();
  policyForm.set("enabled", "on");
  policyForm.set("data_class", "internal");
  policyForm.set("single_data_class_confirmed", "on");
  policyForm.set("ttl_seconds", "1200");
  policyForm.set("charge_cache_hits", "on");
  policyForm.set("tags", "shared, finance");
  assert.deepEqual(responseCachePolicyRequestFromFormData(policyForm, ALIAS_ID, 1), {
    model_alias_id: ALIAS_ID,
    status: "enabled",
    data_class: "internal",
    single_data_class_confirmed: true,
    ttl_seconds: 1200,
    charge_cache_hits: true,
    tags: ["finance", "shared"],
    expected_version: 1,
  });

  policyForm.delete("single_data_class_confirmed");
  assert.throws(
    () => responseCachePolicyRequestFromFormData(policyForm, ALIAS_ID, 1),
    (error) => error instanceof AuthApiError && error.code === "response_cache.input_invalid",
  );

  const invalidation = new FormData();
  invalidation.set("scope", "tags");
  invalidation.append("selector", `policy:${POLICY_ID}`);
  invalidation.append("selector", `model_alias:${ALIAS_ID}`);
  invalidation.set("reason_code", "manual_security");
  assert.deepEqual(responseCacheInvalidationRequestFromFormData(invalidation), {
    scope: "tags",
    match_mode: "all",
    tags: [`model_alias:${ALIAS_ID}`, `policy:${POLICY_ID}`],
    reason_code: "manual_security",
  });
});

test("owner exposes read, write, and invalidate independently and requires exact backend navigation", () => {
  const readContext = authContext(READ, "response_cache_read_only");
  const readOwner = responseCachePageOwner(readContext, shellState(readContext));
  assert.ok(readOwner);
  assert.equal(readOwner.canWrite, false);
  assert.equal(readOwner.canInvalidate, false);
  assert.equal(responseCachePageOwner(readContext, shellState(readContext), { mode: "write" }), null);

  const writeContext = authContext([
    PERMISSIONS.RESPONSE_CACHE_READ,
    PERMISSIONS.RESPONSE_CACHE_WRITE,
  ], "response_cache_write_only");
  const writeOwner = responseCachePageOwner(writeContext, shellState(writeContext));
  assert.equal(writeOwner.canWrite, true);
  assert.equal(writeOwner.canInvalidate, false);

  const invalidateContext = authContext([
    PERMISSIONS.RESPONSE_CACHE_READ,
    PERMISSIONS.RESPONSE_CACHE_INVALIDATE,
  ], "response_cache_invalidate_only");
  const invalidateOwner = responseCachePageOwner(
    invalidateContext,
    shellState(invalidateContext),
  );
  assert.equal(invalidateOwner.canWrite, false);
  assert.equal(invalidateOwner.canInvalidate, true);

  const forgedShell = shellState(readContext);
  forgedShell.navigation.navigation_items[0].required_permissions = [];
  assert.equal(responseCachePageOwner(readContext, forgedShell), null);
  assert.equal(responseCachePageOwner(authContext([]), shellState(authContext([]))), null);
});

test("owner drift clears facts and DOM before abort, and late load cannot revive tenant A", () => {
  const contextA = authContext(ALL, "response_cache_owner_a");
  const ownerA = responseCachePageOwner(contextA, shellState(contextA));
  let state;
  let operation;
  let clearSnapshot;
  let abortedDuringClear = null;
  state = createResponseCachePageState({
    clearDom() {
      clearSnapshot = state.snapshot();
      abortedDuringClear = operation?.controller.signal.aborted ?? false;
    },
  });
  assert.equal(state.enter(ownerA), true);
  operation = state.beginLoad(ownerA);
  assert.ok(operation);

  const contextB = authContext(READ, "response_cache_owner_b");
  const ownerB = responseCachePageOwner(contextB, shellState(contextB));
  assert.equal(state.enter(ownerB), true);
  assert.equal(clearSnapshot.items.length, 0);
  assert.equal(clearSnapshot.editor, null);
  assert.equal(clearSnapshot.invalidateOpen, false);
  assert.equal(abortedDuringClear, false);
  assert.equal(operation.controller.signal.aborted, true);
  assert.equal(state.acceptLoad(operation, listPayload(), ownerA), false);
});

test("dirty editor survives ordinary GET, 409 requires explicit rebase, and unknown result reuses one intent key", () => {
  const context = authContext();
  const owner = responseCachePageOwner(context, shellState(context));
  const state = createResponseCachePageState();
  state.enter(owner);
  const firstLoad = state.beginLoad(owner);
  assert.equal(state.acceptLoad(firstLoad, listPayload(), owner), true);
  state.finishLoad(firstLoad);

  const draft = policyRequest({ ttl_seconds: 1200 });
  assert.equal(state.replaceDraft(draft), true);
  assert.equal(state.snapshot().dirty, true);
  const ordinary = state.beginLoad(owner);
  assert.equal(state.acceptLoad(ordinary, listPayload({
    items: [{ ...listPayload().items[0], cache_policy: policy({ version: 2, ttl_seconds: 1800 }) }],
  }), owner), true);
  state.finishLoad(ordinary);
  assert.equal(state.snapshot().editor.ttl_seconds, 1200);
  assert.equal(state.snapshot().editor.expected_version, 1);

  const firstSave = state.beginSave(owner, draft, () => "response-cache-stable-intent-key");
  assert.ok(firstSave);
  state.rejectSave(firstSave, new AuthApiError({
    status: 409,
    code: "response_cache.version_conflict",
    message: "hostile-server-message",
    requestId: "request_cache_conflict",
  }), owner);
  state.finishSave(firstSave);
  assert.equal(state.snapshot().conflict, true);
  assert.doesNotMatch(state.snapshot().editorError.message, /hostile/);
  assert.equal(state.beginSave(owner, draft, () => "response-cache-conflict-bypass"), null);
  assert.equal(state.replaceDraft(policyRequest({ ttl_seconds: 1300 })), false);
  assert.equal(state.discardDraft(), false);
  const conflictHtml = renderResponseCache(state.snapshot(), context, shellState(context));
  assert.match(conflictHtml, /data-response-cache-rebase/);
  assert.match(conflictHtml, /data-response-cache-chain-id>request_id: request_cache_conflict<\/span>/);
  assert.match(conflictHtml, /name="ttl_seconds"[^>]* disabled/);
  assert.match(conflictHtml, /type="submit" class="primary" disabled/);

  const rebase = state.beginLoad(owner, { rebase: true });
  assert.equal(state.acceptLoad(rebase, listPayload({
    items: [{ ...listPayload().items[0], cache_policy: policy({ version: 2, ttl_seconds: 1800 }) }],
  }), owner), true);
  state.finishLoad(rebase);
  assert.equal(state.snapshot().editor.ttl_seconds, 1200);
  assert.equal(state.snapshot().editor.expected_version, 2);
  assert.equal(state.snapshot().conflict, false);

  const rebasedDraft = { ...state.snapshot().editor };
  const unknown = state.beginSave(owner, rebasedDraft, () => "response-cache-retry-key-a");
  state.rejectSave(unknown, new AuthApiError({
    status: 503,
    code: "response_cache.unavailable",
    message: "network-detail-must-not-render",
    requestId: "request_cache_unknown",
  }), owner);
  state.finishSave(unknown);
  const retry = state.beginSave(owner, rebasedDraft, () => "response-cache-wrong-new-key");
  assert.equal(retry.idempotencyKey, "response-cache-retry-key-a");
  assert.notEqual(retry.generation, unknown.generation);
});

test("invalidate accepts only current-page selectors and keeps write capability independent", () => {
  const context = authContext();
  const owner = responseCachePageOwner(context, shellState(context));
  const state = createResponseCachePageState();
  state.enter(owner);
  const load = state.beginLoad(owner);
  state.acceptLoad(load, listPayload(), owner);
  state.finishLoad(load);
  assert.deepEqual(responseCacheSelectors(state.snapshot().items), [
    `model_alias:${ALIAS_ID}`,
    `policy:${POLICY_ID}`,
    "tag:finance",
  ]);
  assert.equal(state.openInvalidation(owner), true);
  assert.equal(state.beginInvalidation(owner, {
    scope: "tags",
    match_mode: "all",
    tags: ["model_alias:tenant_b_alias"],
    reason_code: "manual_finops",
  }, () => "response-cache-invalidate-key"), null);
  const operation = state.beginInvalidation(owner, {
    scope: "tags",
    match_mode: "all",
    tags: [`model_alias:${ALIAS_ID}`],
    reason_code: "manual_finops",
  }, () => "response-cache-invalidate-key");
  assert.ok(operation);
  state.rejectInvalidation(operation, new AuthApiError({
    status: 503,
    code: "response_cache.unavailable",
    message: "hostile-invalidation-detail",
    requestId: "request_cache_invalidation_unknown",
  }), owner);
  state.finishInvalidation(operation);
  assert.equal(state.snapshot().invalidateState, "unknown_result");
  assert.doesNotMatch(state.snapshot().invalidateError.message, /hostile/);
  const invalidationHtml = renderResponseCache(state.snapshot(), context, shellState(context));
  assert.match(invalidationHtml, /重试同一失效/);
  assert.match(invalidationHtml, /data-response-cache-chain-id>request_id: request_cache_invalidation_unknown<\/span>/);
  const retry = state.beginInvalidation(owner, operation.request, () => "wrong-new-key");
  assert.equal(retry.idempotencyKey, "response-cache-invalidate-key");
  assert.equal(state.acceptInvalidation(retry, {
    invalidation: {
      matched_entry_count: 3,
      deleted_entry_count: 3,
      fenced_fill_count: 1,
    },
    idempotent_replay: false,
    request_id: "request_cache_invalidation",
  }, owner), true);
  state.finishInvalidation(retry);
  assert.equal(state.snapshot().invalidateOpen, false);
  assert.equal(state.snapshot().invalidateFeedback.deletedEntryCount, 3);
});

test("renderer and shell expose only authorized policy facts and never cache payload fields", () => {
  const context = authContext();
  const shell = shellState(context);
  const state = createResponseCachePageState();
  const owner = responseCachePageOwner(context, shell);
  state.enter(owner);
  const load = state.beginLoad(owner);
  state.acceptLoad(load, listPayload(), owner);
  state.finishLoad(load);
  const html = renderResponseCache(state.snapshot(), context, shell);
  assert.match(html, /响应缓存/);
  assert.match(html, /chat-default/);
  assert.match(html, /失效缓存/);
  assert.doesNotMatch(
    html,
    /PRIVATE_CANARY|prompt_text|response_text|ciphertext|cache_key|credential_id|Bearer|fixture-session-token/i,
  );

  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: context,
    consoleShellState: shell,
    responseCacheState: state.snapshot(),
  });
  assert.match(container.innerHTML, /response-cache-panel/);

  const readContext = authContext(READ, "response_cache_read_renderer");
  const readShell = shellState(readContext);
  const readState = createResponseCachePageState();
  const readOwner = responseCachePageOwner(readContext, readShell);
  readState.enter(readOwner);
  const readLoad = readState.beginLoad(readOwner);
  readState.acceptLoad(readLoad, listPayload(), readOwner);
  const readHtml = renderResponseCache(readState.snapshot(), readContext, readShell);
  assert.match(readHtml, /只读/);
  assert.doesNotMatch(readHtml, /失效缓存/);
});

test("RequestLog hit, miss, unavailable, and invalid extra-field projections remain separate from Prompt Cache", () => {
  const hit = normalizeResponseCacheRequestLogInfo(hitProjection(), TENANT_ID);
  assert.equal(hit.hit, true);
  const hitHtml = renderResponseCacheEvidence(hitProjection(), TENANT_ID);
  assert.match(hitHtml, /Response Cache/);
  assert.match(hitHtml, /源请求生成成本/);
  assert.match(hitHtml, /当前参考成本/);
  assert.match(hitHtml, /本次节省/);
  assert.match(hitHtml, /本次计费/);
  assert.match(hitHtml, /¥0\.010000/);
  assert.match(hitHtml, /¥0\.012300/);
  assert.match(hitHtml, /¥0\.000000/);
  assert.doesNotMatch(hitHtml, /Prompt Cache/);

  const miss = {
    ...hitProjection(),
    status: "fill_stored",
    hit: false,
    source_generation_cost_status: "not_applicable",
    source_generation_cost_cny: null,
    current_reference_cost_status: "not_applicable",
    current_reference_cost_cny: null,
    saved_cost_status: "not_applicable",
    saved_cost_cny: null,
    billed_cost_status: "not_applicable",
    billed_cost_cny: null,
  };
  assert.equal(normalizeResponseCacheRequestLogInfo(miss, TENANT_ID)?.hit, false);
  assert.match(renderResponseCacheEvidence(miss, TENANT_ID), /未命中 \/ 旁路/);

  const unavailable = hitProjection({
    current_reference_cost_status: "unavailable",
    current_reference_cost_cny: null,
    saved_cost_status: "unavailable",
    saved_cost_cny: null,
  });
  assert.match(renderResponseCacheEvidence(unavailable, TENANT_ID), /证据不可用/);

  const invalid = { ...hitProjection(), response_text: "PRIVATE_CANARY_MUST_NOT_RENDER" };
  assert.equal(normalizeResponseCacheRequestLogInfo(invalid, TENANT_ID), undefined);
  const invalidHtml = renderResponseCacheEvidence(invalid, TENANT_ID);
  assert.match(invalidHtml, /response_cache\.response_invalid/);
  assert.doesNotMatch(invalidHtml, /PRIVATE_CANARY_MUST_NOT_RENDER/);
  assert.equal(normalizeResponseCacheRequestLogInfo(hitProjection({ tenant_id: "tenant_cache_b" }), TENANT_ID), undefined);

  const missing = hitProjection();
  delete missing.billed_cost_cny;
  assert.equal(normalizeResponseCacheRequestLogInfo(missing, TENANT_ID), undefined);
  const invalidAmount = hitProjection({ saved_cost_cny: "0.012299" });
  assert.equal(normalizeResponseCacheRequestLogInfo(invalidAmount, TENANT_ID), undefined);
  const invalidAmountHtml = renderResponseCacheEvidence(invalidAmount, TENANT_ID);
  assert.match(invalidAmountHtml, /response_cache\.response_invalid/);
  assert.doesNotMatch(invalidAmountHtml, /0\.012299/);
});

test("PAGE-106 main wiring and CSS freeze owner cleanup plus 1280/390 responsive constraints", async () => {
  const [main, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(main, /createResponseCachePageState/);
  assert.match(main, /loadResponseCacheState/);
  assert.match(main, /clearResponseCacheStateOnIdentityChange/);
  assert.match(main, /responseCachePageState\.blocksShellDomReplacement\(\)/);
  assert.match(main, /itemId !== "response-cache"/);
  assert.match(main, /nextItemId !== "response-cache"/);
  assert.match(css, /\.response-cache-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1\.35fr\) minmax\(300px, 0\.65fr\)/s);
  assert.match(css, /\.response-cache-row\s*\{[^}]*grid-template-columns:[^;]*minmax\(46px, 0\.5fr\) 36px;/s);
  assert.match(css, /\.icon-command\s*\{[^}]*width:\s*36px;[^}]*min-width:\s*36px;/s);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.response-cache-row\.is-head\s*\{[^}]*display:\s*none/s);
  assert.match(css, /@media \(max-width: 390px\)[\s\S]*?\.response-cache-dialog-actions button/s);
  assert.match(css, /max-height:\s*calc\(100vh - 20px\)/);
});
