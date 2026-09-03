/**
 * PRD-077 PAGE-107 Console 专项。
 * 固定样本只验证 typed claim/source、owner 与 DOM 安全；问题和回答 canary 不写文件、日志或证据。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  adminAssistantPageOwner,
  createAdminAssistantPageState,
} from "../../src/features/admin-assistant/adminAssistantPageState.js";
import { bindAdminAssistantInteractions } from "../../src/features/admin-assistant/bindAdminAssistant.js";
import {
  hydrateAdminAssistantText,
  renderAdminAssistant,
} from "../../src/features/admin-assistant/renderAdminAssistant.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  ADMIN_ASSISTANT_SOURCE_DEFINITIONS,
  adminAssistantSourceAccess,
  normalizeAdminAssistantResponse,
  queryAdminAssistant,
} from "../../src/lib/adminAssistant.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 固定租户、页面权限和来源权限只用于构造内存样本，测试不会把问题或回答写入证据文件。
const TENANT_ID = "tenant_assistant_a";
const PAGE_PERMISSION = PERMISSIONS.ADMIN_ASSISTANT_QUERY;
const ALL_PERMISSIONS = [
  PERMISSIONS.WORKSPACE_CONSOLE_ASSISTANT_QUERY,
  PAGE_PERMISSION,
  PERMISSIONS.DASHBOARD_READ,
  PERMISSIONS.BILLING_USAGE_READ,
];


test("tampered source index reaches the fail-closed source handler", async () => {
  let clickHandler = null;
  const link = {
    dataset: { adminAssistantSourceIndex: "01" },
    addEventListener(type, handler) {
      if (type === "click") clickHandler = handler;
    },
    getAttribute(name) {
      return name === "href" ? "/#overview" : null;
    },
  };
  const root = {
    querySelector() {
      return null;
    },
    querySelectorAll(selector) {
      return selector === "a[data-admin-assistant-source-index]" ? [link] : [];
    },
  };
  const calls = [];
  assert.equal(bindAdminAssistantInteractions({
    async onOpenSource(index, href) {
      calls.push([index, href]);
    },
  }, root), true);
  assert.equal(typeof clickHandler, "function");
  await clickHandler({ preventDefault() {} });
  assert.deepEqual(calls, [[null, "/#overview"]]);
});

/** 构造带服务端权限快照的认证上下文，允许用例精确制造租户或 revision 漂移。 */
function authContext(overrides = {}) {
  const tenantId = overrides.tenantId ?? TENANT_ID;
  // 权限结论来自显式快照而不是 role 文本，确保测试能证明前端不按角色名放行。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, display_name: "Assistant tenant" },
    user: {
      tenant_id: tenantId,
      user_id: overrides.userId ?? "usr_assistant_a",
      email: "assistant@example.test",
      role: overrides.role ?? "finance_admin",
    },
    session: {
      session_id: overrides.sessionId ?? "ses_assistant_a",
      session_revision: overrides.sessionRevision ?? 3,
    },
  }, overrides.permissions ?? ALL_PERMISSIONS, {
    permissionRevision: overrides.permissionRevision ?? 13,
    authorizationFingerprint: overrides.fingerprint ?? "assistant_fingerprint_13",
  });
}

/** 构造与认证快照一致的后端导航状态，并可移除来源入口验证链接降级。 */
function shellState(context = authContext(), { includeSources = true } = {}) {
  const navigationItems = [{
    id: "admin-assistant",
    target_section: "admin-assistant",
    label: "管理助手",
    group: "总览",
    visible: true,
    disabled: false,
    required_permissions: [PAGE_PERMISSION],
  }];
  if (includeSources) {
    navigationItems.push(
      {
        id: "overview",
        target_section: "overview",
        label: "概览",
        group: "总览",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.DASHBOARD_READ],
      },
      {
        id: "usage",
        target_section: "usage",
        label: "用量",
        group: "调用与成本",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.BILLING_USAGE_READ],
      },
    );
  }
  return {
    state: "ok",
    activeNavigationItemId: "admin-assistant",
    navigation: {
      active_navigation_item_id: "admin-assistant",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: navigationItems,
    },
  };
}

/** 生成完整 typed answer/source 图，所有 ID 与公开文案均为固定非敏感样本。 */
function successPayload(overrides = {}) {
  return {
    assistant_query_id: `asq_${"a".repeat(32)}`,
    assistant_audit_id: `asa_${"b".repeat(32)}`,
    query_type: "usage_statistics",
    answer: {
      summary: "基于 2 个可验证来源生成。",
      claims: [
        {
          claim_id: "request_count",
          claim_type: "integer",
          label: "调用次数<script>claimCanary()</script>",
          value: 120,
          unit: "requests",
          source_ids: ["src_overview"],
        },
        {
          claim_id: "usage_cost",
          claim_type: "decimal",
          label: "用量成本",
          value: "12.340000",
          unit: "CNY",
          source_ids: ["src_usage"],
        },
      ],
      recommendations: [{
        code: "insufficient_evidence",
        message: "当前证据不足以给出优化建议。",
        source_ids: ["src_usage"],
      }],
    },
    sources: [
      {
        source_id: "src_overview",
        source_type: "overview",
        label: "运行概览",
        href: "/#overview",
        record_count: 120,
        window_started_at: "2026-08-04T00:00:00Z",
        window_ended_at: "2026-08-11T00:00:00Z",
        snapshot_at: "2026-08-11T00:00:01Z",
      },
      {
        source_id: "src_usage",
        source_type: "usage",
        label: "用量账本",
        href: "/#usage",
        record_count: 120,
        window_started_at: "2026-08-04T00:00:00Z",
        window_ended_at: "2026-08-11T00:00:00Z",
        snapshot_at: "2026-08-11T00:00:01Z",
      },
    ],
    redactions: ["content_omitted", "credentials_omitted"],
    request_id: "req_assistant_success",
    trace_id: "trc_assistant_success",
    ...overrides,
  };
}

/** 模拟只暴露 JSON 与冻结私有 Header 的 Fetch 响应，不提供原始响应正文旁路。 */
function httpResponse(payload, { status = 200, headers = {} } = {}) {
  const requestId = payload?.request_id ?? "req_error";
  const traceId = payload?.trace_id ?? "trc_error";
  const responseHeaders = new Map(Object.entries({
    "x-request-id": requestId,
    "x-trace-id": traceId,
    "cache-control": "no-store, private",
    pragma: "no-cache",
    vary: "Authorization",
    ...Object.fromEntries(Object.entries(headers).map(([name, value]) => [name.toLowerCase(), value])),
  }));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get(name) {
        return responseHeaders.get(String(name).toLowerCase()) ?? null;
      },
    },
    async json() { return payload; },
  };
}

test("client sends exact question-only no-store POST and accepts the frozen claim/source graph", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return httpResponse(successPayload());
  };
  const privateQuestion = "<script>questionCanary()</script> 过去 7 天调用情况如何？";
  const result = await queryAdminAssistant({
    controlApiUrl: "http://control.local",
    token: "session-token",
    question: privateQuestion,
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://control.local/admin-assistant/query");
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), { question: privateQuestion });
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.headers.Authorization, "Bearer session-token");
  assert.equal("Idempotency-Key" in calls[0].options.headers, false);
  assert.doesNotMatch(calls[0].options.body, /tenant_id|role|permission_codes|source_type|href/);
  assert.equal(result.answer.claims.length, 2);
  assert.equal(result.sources[0].href, "/#overview");
  assert.equal(Object.isFrozen(result), true);
});

test("client never retries an uncertain POST and folds the private network exception", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    // 私有 canary 模拟网络异常对象，断言客户端只能返回固定公开错误而不能泄漏异常正文。
    throw new Error("private-proxy-url questionCanary responseCanary");
  };
  await assert.rejects(
    () => queryAdminAssistant({
      controlApiUrl: "http://control.local",
      token: "token",
      question: "解释最近 24 小时的主要错误。",
    }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.code, "admin_assistant.network_unavailable");
      assert.equal(error.status, 0);
      assert.doesNotMatch(error.message, /proxy|Canary/);
      return true;
    },
  );
  assert.equal(calls, 1);
});

test("client rejects unsafe questions, unknown response keys, typed drift and arbitrary source URLs", async () => {
  await assert.rejects(
    () => queryAdminAssistant({ controlApiUrl: "http://control.local", token: "token", question: "bad\u202equery" }),
    (error) => error.code === "admin_assistant.request_invalid",
  );
  assert.equal(normalizeAdminAssistantResponse({ ...successPayload(), tenant_id: TENANT_ID }), null);

  const floatDecimal = structuredClone(successPayload());
  floatDecimal.answer.claims[1].value = 12.34;
  assert.equal(normalizeAdminAssistantResponse(floatDecimal), null);

  const external = structuredClone(successPayload());
  external.sources[0].href = "https://example.com/#overview";
  assert.equal(normalizeAdminAssistantResponse(external), null);

  const orphan = structuredClone(successPayload());
  orphan.answer.claims[0].source_ids = ["src_usage"];
  assert.equal(normalizeAdminAssistantResponse(orphan), null);

  const missingRedaction = structuredClone(successPayload());
  missingRedaction.redactions = ["content_omitted"];
  assert.equal(normalizeAdminAssistantResponse(missingRedaction), null);

  const nullableUnit = structuredClone(successPayload());
  nullableUnit.answer.claims[0].unit = null;
  assert.ok(normalizeAdminAssistantResponse(nullableUnit));

  const longLabel = structuredClone(successPayload());
  longLabel.answer.claims[0].label = "L".repeat(81);
  assert.equal(normalizeAdminAssistantResponse(longLabel), null);

  const unsafeInteger = structuredClone(successPayload());
  unsafeInteger.answer.claims[0].value = Number.MAX_SAFE_INTEGER + 1;
  assert.equal(normalizeAdminAssistantResponse(unsafeInteger), null);

  const longText = structuredClone(successPayload());
  longText.answer.claims[0] = {
    ...longText.answer.claims[0],
    claim_type: "text",
    value: "T".repeat(513),
    unit: null,
  };
  assert.equal(normalizeAdminAssistantResponse(longText), null);
});

test("client requires exact private headers and matching chains on success and errors", async () => {
  const errorPayload = {
    code: "auth.permission_denied",
    message: "权限不足",
    request_id: "req_assistant_denied",
    trace_id: "trc_assistant_denied",
    suggestion: "",
    diagnosis: null,
  };
  const cases = [
    { headers: { "cache-control": "no-store" } },
    { headers: { pragma: "" } },
    { headers: { vary: "Origin" } },
    { headers: { vary: "Authorization, Accept-Encoding" } },
    { headers: { "x-request-id": "req_mismatch" } },
    { headers: { "x-trace-id": "" } },
  ];
  for (const options of cases) {
    globalThis.fetch = async () => httpResponse(successPayload(), options);
    await assert.rejects(
      () => queryAdminAssistant({
        controlApiUrl: "http://control.local",
        token: "token",
        question: "最近 7 天用量如何？",
      }),
      (error) => error instanceof AuthApiError && error.code === "admin_assistant.response_invalid",
    );
  }

  globalThis.fetch = async () => httpResponse(errorPayload, {
    status: 403,
    headers: { vary: "Authorization, Origin" },
  });
  await assert.rejects(
    () => queryAdminAssistant({
      controlApiUrl: "http://control.local",
      token: "token",
      question: "最近 7 天用量如何？",
    }),
    (error) => error instanceof AuthApiError
      && error.code === "auth.permission_denied"
      && error.requestId === errorPayload.request_id,
  );

  globalThis.fetch = async () => httpResponse(errorPayload, {
    status: 403,
    headers: { "cache-control": "public" },
  });
  await assert.rejects(
    () => queryAdminAssistant({
      controlApiUrl: "http://control.local",
      token: "token",
      question: "最近 7 天用量如何？",
    }),
    (error) => error instanceof AuthApiError && error.code === "admin_assistant.response_invalid",
  );
});

test("the ten source triples are exact and link access intersects latest permission navigation", () => {
  assert.deepEqual(Object.keys(ADMIN_ASSISTANT_SOURCE_DEFINITIONS).sort(), [
    "budgets",
    "cost_anomalies",
    "finops_ledger",
    "model_prices",
    "overview",
    "policies",
    "policy_snapshots",
    "request_explanations",
    "request_logs",
    "usage",
  ]);
  const context = authContext();
  const shell = shellState(context);
  const source = normalizeAdminAssistantResponse(successPayload()).sources[0];
  assert.deepEqual(adminAssistantSourceAccess(source, context, shell), {
    allowed: true,
    href: "/#overview",
    navigationId: "overview",
    permission: PERMISSIONS.DASHBOARD_READ,
  });
  assert.equal(adminAssistantSourceAccess(source, context, shellState(context, { includeSources: false })).allowed, false);
  const stale = shellState(context);
  stale.navigation.permission_revision += 1;
  assert.equal(adminAssistantSourceAccess(source, context, stale).allowed, false);
  const forgedTarget = shellState(context);
  forgedTarget.navigation.navigation_items[1].target_section = "usage";
  assert.equal(adminAssistantSourceAccess(source, context, forgedTarget).allowed, false);
});

test("page owner requires exact auth/navigation mirror and never derives access from role", () => {
  const context = authContext();
  assert.ok(adminAssistantPageOwner(context, shellState(context)));
  const noPermission = authContext({ permissions: [], role: "admin" });
  assert.equal(adminAssistantPageOwner(noPermission, shellState(noPermission)), null);
  const forged = shellState(context);
  forged.navigation.navigation_items[0].required_permissions = [];
  assert.equal(adminAssistantPageOwner(context, forged), null);
  const wrongTenant = authContext({ tenantId: "tenant_assistant_b" });
  wrongTenant.tenant.tenant_id = TENANT_ID;
  assert.equal(adminAssistantPageOwner(wrongTenant, shellState(wrongTenant)), null);
});

test("generation plus owner drift aborts before DOM clear and late success cannot revive tenant A", () => {
  const contextA = authContext();
  const ownerA = adminAssistantPageOwner(contextA, shellState(contextA));
  let ticket;
  let abortedAtDomClear = false;
  const state = createAdminAssistantPageState({
    clearDom() { abortedAtDomClear = ticket?.signal.aborted ?? false; },
  });
  assert.equal(state.enter(ownerA), true);
  assert.equal(state.setDraft(ownerA, "tenant-a-question-canary"), true);
  ticket = state.beginQuery(ownerA);
  assert.ok(ticket);
  assert.equal(state.beginQuery(ownerA), null);

  const contextB = authContext({
    tenantId: "tenant_assistant_b",
    userId: "usr_assistant_b",
    sessionId: "ses_assistant_b",
    permissionRevision: 14,
    fingerprint: "assistant_fingerprint_14",
  });
  const ownerB = adminAssistantPageOwner(contextB, shellState(contextB));
  assert.equal(state.enter(ownerB), true);
  assert.equal(ticket.signal.aborted, true);
  assert.equal(abortedAtDomClear, true);
  assert.equal(state.acceptQuery(ticket, normalizeAdminAssistantResponse(successPayload()), ownerA), false);
  assert.equal(state.snapshot().draft, "");
  assert.equal(state.snapshot().response, null);
});

test("cancel, clear and uncertain states keep one attempt and no hidden history", () => {
  const context = authContext();
  const owner = adminAssistantPageOwner(context, shellState(context));
  const state = createAdminAssistantPageState();
  state.enter(owner);
  state.setDraft(owner, "single-question");
  const canceled = state.beginQuery(owner);
  assert.equal(state.cancel(owner), true);
  assert.equal(canceled.signal.aborted, true);
  assert.equal(state.snapshot().status, "draft");

  const uncertain = state.beginQuery(owner);
  const error = new AuthApiError({
    status: 0,
    code: "admin_assistant.network_unavailable",
    message: "管理助手响应状态未知",
    requestId: "-",
    traceId: "-",
    suggestion: "",
    diagnosis: null,
  });
  assert.equal(state.rejectQuery(uncertain, error, owner), true);
  assert.equal(state.snapshot().status, "uncertain");
  assert.equal(state.snapshot().response, null);
  assert.equal(state.clear(owner), true);
  assert.deepEqual(state.snapshot(), {
    status: "idle",
    draft: "",
    response: null,
    error: null,
    busy: false,
    canSubmit: false,
    hasContent: false,
  });
});

test("renderer has zero DOM without owner and keeps all question/answer text out of HTML", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = adminAssistantPageOwner(context, shell);
  const state = createAdminAssistantPageState();
  state.enter(owner);
  const privateQuestion = "<script>privateQuestionCanary()</script>";
  state.setDraft(owner, privateQuestion);
  const ticket = state.beginQuery(owner);
  const response = normalizeAdminAssistantResponse(successPayload());
  state.acceptQuery(ticket, response, owner);

  const html = renderAdminAssistant(state.snapshot(), context, shell);
  assert.match(html, /data-admin-assistant-root/);
  assert.match(html, /href="\/#overview"/);
  assert.doesNotMatch(html, /privateQuestionCanary|claimCanary/);
  assert.doesNotMatch(html, /<script>claim/);

  const noSourcesHtml = renderAdminAssistant(
    state.snapshot(),
    context,
    shellState(context, { includeSources: false }),
  );
  assert.doesNotMatch(noSourcesHtml, /href="\/#overview"/);
  assert.match(noSourcesHtml, /admin-assistant-source-static/);
  const denied = authContext({ permissions: [], role: "admin" });
  assert.equal(renderAdminAssistant({}, denied, shellState(denied)), "");
});

test("hydration uses textContent/value and never parses service text as markup", () => {
  const context = authContext();
  const owner = adminAssistantPageOwner(context, shellState(context));
  const state = createAdminAssistantPageState();
  state.enter(owner);
  state.setDraft(owner, "<textarea>questionCanary</textarea>");
  const ticket = state.beginQuery(owner);
  state.acceptQuery(ticket, normalizeAdminAssistantResponse(successPayload()), owner);
  const snapshot = state.snapshot();

  const nodes = Object.fromEntries([
    "live", "errorMessage", "errorCode", "errorRequest", "summary", "queryId", "auditId", "requestId", "traceId",
  ].map((name) => [name, { textContent: "" }]));
  const textarea = { value: "", disabled: false };
  const submit = { disabled: false };
  const cancel = { disabled: false };
  const clear = { disabled: false };
  const result = { setAttribute(name, value) { this[name] = value; } };
  const claimLabel = { textContent: "" };
  const claimValue = { textContent: "" };
  const claim = {
    getAttribute: () => "0",
    querySelector(selector) {
      return selector === "[data-admin-assistant-claim-label]" ? claimLabel : claimValue;
    },
  };
  const recommendationMessage = { textContent: "" };
  const recommendation = {
    getAttribute: () => "0",
    querySelector: () => recommendationMessage,
  };
  const sourceLabel = { textContent: "" };
  const sourceDetail = { textContent: "" };
  const source = {
    getAttribute: () => "0",
    querySelector(selector) {
      return selector === "[data-admin-assistant-source-label]" ? sourceLabel : sourceDetail;
    },
  };
  const selectorNodes = new Map([
    ["[data-admin-assistant-question]", textarea],
    ["[data-admin-assistant-submit]", submit],
    ["[data-admin-assistant-cancel]", cancel],
    ["[data-admin-assistant-clear]", clear],
    [".admin-assistant-result", result],
    ["[data-admin-assistant-live]", nodes.live],
    ["[data-admin-assistant-error-message]", nodes.errorMessage],
    ["[data-admin-assistant-error-code]", nodes.errorCode],
    ["[data-admin-assistant-error-request]", nodes.errorRequest],
    ["[data-admin-assistant-summary]", nodes.summary],
    ["[data-admin-assistant-query-id]", nodes.queryId],
    ["[data-admin-assistant-audit-id]", nodes.auditId],
    ["[data-admin-assistant-request-id]", nodes.requestId],
    ["[data-admin-assistant-trace-id]", nodes.traceId],
  ]);
  const page = {
    dataset: {},
    matches: (selector) => selector === "[data-admin-assistant-root]",
    querySelector: (selector) => selectorNodes.get(selector) ?? null,
    querySelectorAll(selector) {
      if (selector === "[data-admin-assistant-claim-index]") return [claim];
      if (selector === "[data-admin-assistant-recommendation-index]") return [recommendation];
      if (selector === "[data-admin-assistant-source-index]") return [source];
      return [];
    },
  };
  assert.equal(hydrateAdminAssistantText(page, snapshot), true);
  assert.equal(textarea.value, "<textarea>questionCanary</textarea>");
  assert.equal(nodes.summary.textContent, "基于 2 个可验证来源生成。");
  assert.match(claimLabel.textContent, /claimCanary/);
  assert.equal(recommendationMessage.textContent, "当前证据不足以给出优化建议。");
  assert.equal(sourceLabel.textContent, "运行概览");
  assert.equal(nodes.queryId.textContent, `asq_${"a".repeat(32)}`);
});

test("existing Console shell renders PAGE-107 only from authorized backend navigation", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = adminAssistantPageOwner(context, shell);
  const state = createAdminAssistantPageState();
  state.enter(owner);
  state.setDraft(owner, "shell-private-question-canary");
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: context,
    consoleShellState: shell,
    adminAssistantState: state.snapshot(),
  });
  assert.match(container.innerHTML, /admin-assistant-panel/);
  assert.match(container.innerHTML, /data-console-experience-assistant-form/);
  assert.doesNotMatch(container.innerHTML, /shell-private-question-canary/);
});

test("main lifecycle and responsive CSS freeze revoke cleanup, no prefetch and 1280/390 layout", async () => {
  const [main, client, pageState, renderer, binder, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/adminAssistant.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/admin-assistant/adminAssistantPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/admin-assistant/renderAdminAssistant.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/admin-assistant/bindAdminAssistant.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(main, /addEventListener\("hashchange"[\s\S]*adminAssistantPageState\.leave\(\)[\s\S]*bindNavigationHashRefresh/);
  assert.doesNotMatch(main, /nextItemId !== "admin-assistant"[\s\S]{0,160}adminAssistantPageState\.leave\(\)/);
  assert.match(main, /pagehide[\s\S]*adminAssistantPageState\.leave\(\)/);
  assert.match(main, /clearAdminAssistantStateOnIdentityChange\(authSession\.context, context\)/);
  assert.match(main, /isBackgroundRefresh[\s\S]*adminAssistantPageState\.blocksShellDomReplacement\(\)/);
  assert.doesNotMatch(main, /window\.setInterval\(\(\) => \{[\s\S]*?void refresh\(\);/);
  assert.match(main, /itemId !== "admin-assistant"[\s\S]*adminAssistantPageState\.leave\(\)/);
  assert.match(main, /adminAssistantPageState\.leave\(\)[\s\S]*switchConsoleNavigation/);
  const shellLoadIndex = main.indexOf("const consoleShellState = await loadConsoleShellState();");
  const shellOwnerGuardIndex = main.indexOf("if (!refreshOwnerCurrent())", shellLoadIndex);
  const firstShellMutationIndex = main.indexOf(
    'if (consoleShellState?.activeNavigationItemId !== "enterprise-chat")',
    shellLoadIndex,
  );
  assert.ok(shellLoadIndex >= 0);
  assert.ok(shellOwnerGuardIndex > shellLoadIndex);
  assert.ok(firstShellMutationIndex > shellOwnerGuardIndex);
  assert.match(
    main,
    /!workspaceConsoleExperienceView\(consoleShellState\?\.activeNavigationItemId\)[\s\S]*adminAssistantPageState\.blocksShellDomReplacement/,
  );
  const assistantLifecycleIndex = main.indexOf(
    'if (consoleShellState?.activeNavigationItemId !== "admin-assistant")',
    firstShellMutationIndex,
  );
  const lateOwnerGuardIndex = main.lastIndexOf(
    "if (!refreshOwnerCurrent())",
    assistantLifecycleIndex,
  );
  assert.ok(assistantLifecycleIndex > firstShellMutationIndex);
  assert.ok(lateOwnerGuardIndex > firstShellMutationIndex);
  assert.ok(lateOwnerGuardIndex < assistantLifecycleIndex);
  assert.match(
    main,
    /activeConsoleNavigationCount \+= 1;[\s\S]*?try \{[\s\S]*?finally \{\s*activeConsoleNavigationCount -= 1;/,
  );
  assert.match(main, /case "admin-assistant":[\s\S]*loadWorkspaceConsoleExperienceState\([\s\S]*"admin-assistant"/);
  assert.match(main, /function loadWorkspaceConsoleExperienceState[\s\S]*workspaceConsoleExperienceController\.enter/);
  assert.match(main, /onAssistantSubmit\(\)[\s\S]*workspaceConsoleExperienceController\.submitAssistant\(\)/);
  assert.equal((main.match(/queryWorkspaceConsoleAssistant\(/g) ?? []).length, 1);

  const implementation = `${client}\n${pageState}\n${renderer}\n${binder}`;
  assert.doesNotMatch(implementation, /localStorage|sessionStorage|indexedDB|caches\.open|console\.(?:log|error|warn)|location\.(?:hash|href)/);
  assert.match(renderer, /\.textContent\s*=/);
  assert.match(renderer, /textarea\.value\s*=/);
  assert.doesNotMatch(renderer, /innerHTML\s*=/);
  assert.match(css, /\.admin-assistant-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(300px, 0\.82fr\) minmax\(0, 1\.18fr\)/s);
  assert.match(css, /@media \(max-width: 900px\)[\s\S]*?\.admin-assistant-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/s);
  assert.match(css, /@media \(max-width: 390px\)[\s\S]*?\.admin-assistant-form-actions/s);
  assert.match(css, /\.admin-assistant-icon-command\s*\{[^}]*width:\s*36px;[^}]*min-width:\s*36px;[^}]*height:\s*36px/s);
  assert.match(css, /\.admin-assistant-sources strong,[\s\S]*overflow-wrap:\s*anywhere/);
});
