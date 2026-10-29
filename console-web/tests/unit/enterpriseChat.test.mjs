// PRD-066 PAGE-093 专项覆盖 API exact-set、内存正文、完整 owner、迟到响应、XSS、权限和刷新清理。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createEnterpriseChatPageState,
  enterpriseChatIdentitySnapshot,
  enterpriseChatTemplateUseIdentitySnapshot,
} from "../../src/features/enterprise-chat/enterpriseChatPageState.js";
import {
  hydrateEnterpriseChatText,
  renderEnterpriseChat,
} from "../../src/features/enterprise-chat/renderEnterpriseChat.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  clearEnterpriseChatSession,
  createEnterpriseChatClientMessageId,
  createEnterpriseChatSession,
  getEnterpriseChatOptions,
  sendEnterpriseChatMessage,
} from "../../src/lib/enterpriseChat.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造包含 tenant、用户、登录 Session、revision 和授权指纹的真实形状 owner。 */
function chatAuth(overrides = {}) {
  return withPermissionSnapshot({
    user: {
      tenant_id: overrides.tenantId ?? "tenant_chat_a",
      user_id: overrides.userId ?? "usr_chat_a",
      email: "viewer@example.com",
      role: overrides.role ?? "viewer",
    },
    tenant: { tenant_id: overrides.tenantId ?? "tenant_chat_a", name: "Chat Tenant" },
    session: {
      session_id: overrides.sessionId ?? "ses_chat_a",
      session_revision: overrides.sessionRevision ?? 3,
    },
  }, overrides.permissions ?? [PERMISSIONS.CHAT_USE], {
    permissionRevision: overrides.permissionRevision ?? 7,
    authorizationFingerprint: overrides.fingerprint ?? "chat_fingerprint_7",
  });
}

/** 构造 options 安全目录；测试可收紧限制，额外敏感字段用于证明状态机会丢弃未知响应字段。 */
function optionsPayload(limitOverrides = {}) {
  return {
    options_revision: "catalog_chat_1",
    projects: [
      {
        project_id: "prj_chat_1",
        name: "客服项目",
        environment_id: "env_prod_1",
        environment_name: "生产",
        availability: "ready",
        virtual_key_id: "vk_must_not_reach_state",
      },
      {
        project_id: "prj_chat_2",
        name: "多 Key 项目",
        environment_id: "env_prod_1",
        environment_name: "生产",
        availability: "ambiguous_key",
      },
    ],
    model_aliases: [
      {
        model_alias_id: "mal_chat_1",
        alias: "enterprise-chat",
        display_name: "企业问答模型",
        environment_id: "env_prod_1",
        provider_id: "provider_must_not_reach_state",
      },
    ],
    prompt_templates: [{
      prompt_template_id: "ptpl_chat_customer_reply",
      prompt_template_version_id: "ptv_chat_customer_reply_v1",
      version_number: 1,
      name: "客户回复",
      variables: [{ name: "customer_name", label: "客户名称", required: true, max_chars: 80 }],
      project_scope: "selected",
      applicable_project_ids: ["prj_chat_1"],
      recommended_model_alias_ids: ["mal_chat_1"],
      content: "prompt-content-must-not-reach-chat-options",
    }],
    limits: {
      max_history_messages: 20,
      max_message_chars: 8000,
      max_history_bytes: 65536,
      session_ttl_seconds: 28800,
      ...limitOverrides,
    },
    request_id: "req_options_1",
    trace_id: "trc_options_1",
    tenant_id: "tenant_must_not_reach_state",
  };
}

/** 建立已经加载 options 并创建后端短会话的页面状态；可注入更小限制验证失败关闭。 */
function initializedChatState(limitOverrides = {}) {
  const state = createEnterpriseChatPageState();
  const auth = chatAuth();
  assert.equal(state.syncOwner(auth, "enterprise-chat"), true);
  const optionsTicket = state.beginOptions();
  assert.ok(optionsTicket);
  assert.equal(state.acceptOptions(optionsTicket, optionsPayload(limitOverrides)), true);
  const createTicket = state.beginCreateSession();
  assert.ok(createTicket);
  assert.equal(state.acceptSession(createTicket, {
    session: {
      chat_session_id: "chs_1234567890abcdef1234567890abcdef",
      project_id: "prj_chat_1",
      project_name: "客服项目",
      environment_id: "env_prod_1",
      environment_name: "生产",
      model_alias_id: "mal_chat_1",
      model_alias: "enterprise-chat",
      state_revision: 1,
      expires_at: "2026-08-02T08:00:00Z",
      virtual_key_id: "vk_must_not_reach_state",
    },
  }), true);
  // 返回认证上下文只供后续 owner 变更断言使用，敏感 Virtual Key 必须已被状态机丢弃。
  return { state, auth };
}

/** 构造最小 fetch Response stub，只暴露客户端契约允许读取的状态、响应头与 JSON 接口。 */
function response({ status = 200, payload = {}, headers = {} } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headers[name] ?? headers[name.toLowerCase()] ?? null },
    async json() { return payload; },
  };
}

test("enterprise Chat client sends exact public contracts without tenant, Key, Provider or model text", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (options.method === "DELETE") return response({ status: 204 });
    return response({ payload: { request_id: "req_chat_client", trace_id: "trc_chat_client" } });
  };
  const signal = new AbortController().signal;
  await getEnterpriseChatOptions({ controlApiUrl: "http://control.local", token: "session-token", signal });
  await createEnterpriseChatSession({
    controlApiUrl: "http://control.local",
    token: "session-token",
    projectId: "prj_chat_1",
    environmentId: "env_prod_1",
    modelAliasId: "mal_chat_1",
    signal,
  });
  const privatePrompt = "prompt-private-canary";
  await sendEnterpriseChatMessage({
    controlApiUrl: "http://control.local",
    token: "session-token",
    chatSessionId: "chs_1234567890abcdef1234567890abcdef",
    clientMessageId: "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
    history: [{ role: "user", content: privatePrompt }],
    signal,
  });
  await clearEnterpriseChatSession({
    controlApiUrl: "http://control.local",
    token: "session-token",
    chatSessionId: "chs_1234567890abcdef1234567890abcdef",
    signal,
  });

  assert.equal(calls.length, 4);
  assert.equal(calls[0].url, "http://control.local/chat/options");
  assert.equal(calls[0].options.body, undefined);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    project_id: "prj_chat_1",
    environment_id: "env_prod_1",
    model_alias_id: "mal_chat_1",
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    client_message_id: "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
    history: [{ role: "user", content: privatePrompt }],
  });
  assert.equal(calls[3].options.method, "DELETE");
  assert.equal(calls.every((call) => call.options.headers.Authorization === "Bearer session-token"), true);
  assert.equal(calls.every((call) => call.options.cache === "no-store"), true);
  // Fetch 的 cache mode 已足够禁止缓存；显式 Cache-Control 请求头会触发无法通过的 CORS 预检。
  assert.equal(calls.every((call) => !("Cache-Control" in call.options.headers)), true);
  assert.doesNotMatch(calls.map((call) => call.url).join("\n"), /prompt-private|tenant|virtual.?key|provider/i);
  assert.doesNotMatch(calls[1].options.body, /tenant|user|role|permission|virtual.?key|provider|endpoint|alias\"/i);
  assert.doesNotMatch(calls[2].options.body, /tenant|virtual.?key|provider|endpoint|model/i);
});

test("enterprise Chat client never retries uncertain network failures and does not expose proxy bodies", async () => {
  let callCount = 0;
  globalThis.fetch = async () => {
    callCount += 1;
    // 原始异常故意携带敏感哨兵，用于证明客户端统一错误不会泄露代理地址或 Prompt 正文。
    throw new Error("proxy-private-url-and-response-canary");
  };
  await assert.rejects(
    () => sendEnterpriseChatMessage({
      controlApiUrl: "http://control.local",
      token: "token",
      chatSessionId: "chs_1234567890abcdef1234567890abcdef",
      clientMessageId: "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
      history: [{ role: "user", content: "private-prompt-canary" }],
    }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "chat.gateway_unavailable");
      assert.equal(error.status, 503);
      assert.doesNotMatch(`${error.message} ${error.stack ?? ""}`, /proxy-private-url|private-prompt-canary/);
      return true;
    },
  );
  assert.equal(callCount, 1);

  globalThis.fetch = async () => ({
    ok: false,
    status: 502,
    headers: { get: (name) => name === "X-Request-ID" ? "req_proxy_chat" : null },
    async json() { throw new Error("proxy-html-body-canary"); },
  });
  await assert.rejects(
    () => getEnterpriseChatOptions({ controlApiUrl: "http://control.local", token: "token" }),
    (error) => {
      assert.equal(error.requestId, "req_proxy_chat");
      assert.doesNotMatch(error.message, /proxy-html-body-canary/);
      return true;
    },
  );
});

test("enterprise Chat client message id is a canonical UUID and unsafe path ids never reach fetch", async () => {
  assert.match(createEnterpriseChatClientMessageId(), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  let called = false;
  globalThis.fetch = async () => { called = true; return response(); };
  await assert.rejects(
    () => clearEnterpriseChatSession({
      controlApiUrl: "http://control.local",
      token: "token",
      chatSessionId: "../tenant-other?key=secret",
    }),
    (error) => error instanceof AuthApiError && error.code === "chat.request_invalid",
  );
  assert.equal(called, false);
});

test("enterprise Chat state binds navigation and complete authorization owner before loading", () => {
  const state = createEnterpriseChatPageState();
  assert.equal(state.syncOwner(chatAuth({ permissions: [] }), "enterprise-chat"), false);
  assert.equal(state.beginOptions(), null);
  assert.equal(state.syncOwner(chatAuth(), "overview"), false);
  assert.equal(state.beginOptions(), null);
  assert.equal(state.syncOwner(chatAuth(), "enterprise-chat"), true);
  const ticket = state.beginOptions();
  assert.ok(ticket);
  assert.equal(ticket.signal.aborted, false);
  assert.equal(state.acceptOptions(ticket, optionsPayload()), true);
  const serialized = JSON.stringify(state.snapshot());
  assert.doesNotMatch(serialized, /tenant_must_not|vk_must_not|provider_must_not/);
  assert.equal(state.snapshot().selectedProjectId, "prj_chat_1");
  assert.equal(state.snapshot().selectedModelAliasId, "mal_chat_1");
  assert.equal(state.snapshot().blocksRefresh, undefined);
});

test("enterprise Chat state keeps plaintext only in page memory and accepts a current governed response", () => {
  const { state, auth } = initializedChatState();
  const prompt = "<img src=x onerror=promptCanary()>\n请总结用量";
  const answer = "<script>responseCanary()</script>\n本月调用正常";
  state.setDraft(prompt);
  assert.equal(state.blocksRefresh(), true);
  const ticket = state.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.ok(ticket);
  assert.deepEqual(ticket.history, [{ role: "user", content: prompt }]);
  assert.equal(state.acceptMessage(ticket, {
    chat_session_id: "chs_1234567890abcdef1234567890abcdef",
    chat_message_id: "chm_1234567890abcdef1234567890abcdef",
    client_message_id: ticket.clientMessageId,
    assistant: { role: "assistant", content: answer },
    execution: {
      request_id: "req_chat_send_1",
      trace_id: "trc_chat_send_1",
      input_tokens: 12,
      output_tokens: 8,
      total_tokens: 20,
      cache_tokens: 0,
      cost_cny: "0.001234",
      policy: { decision: "allow", reason_code: "policy.default_allow" },
      fallback_triggered: false,
      cache_status: "not_configured",
      provider_id: "provider-secret",
      endpoint: "https://private.example",
    },
    session_revision: 2,
  }), true);
  const snapshot = state.snapshot();
  assert.equal(snapshot.messages.length, 2);
  assert.equal(snapshot.messages[0].content, prompt);
  assert.equal(snapshot.messages[1].content, answer);
  assert.equal(snapshot.messages[1].execution.total_tokens, 20);
  assert.doesNotMatch(JSON.stringify(snapshot.messages[1].execution), /provider-secret|private\.example/);
  assert.equal(snapshot.draft, "");
  assert.equal(snapshot.session.state_revision, 2);
  assert.equal(enterpriseChatIdentitySnapshot(auth).length > 0, true);
});

test("enterprise Chat reaches the message-count limit with a fixed clear-or-new prompt and disabled send", () => {
  const { state, auth } = initializedChatState({ max_history_messages: 2 });
  state.setDraft("count-limit-private-prompt");
  const ticket = state.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.ok(ticket);
  assert.equal(state.acceptMessage(ticket, {
    chat_session_id: "chs_1234567890abcdef1234567890abcdef",
    client_message_id: ticket.clientMessageId,
    assistant: { role: "assistant", content: "count-limit-private-response" },
    execution: { request_id: "req_count_limit", trace_id: "trc_count_limit" },
    session_revision: 2,
  }), true);

  state.setDraft("must-not-be-sent-after-count-limit");
  const snapshot = state.snapshot();
  assert.equal(snapshot.historyLimitReached, true);
  assert.equal(snapshot.historyLimitMessage, "当前会话已达到上下文上限，请清空或新建会话");
  assert.equal(state.beginSend("e4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0"), null);

  const html = renderEnterpriseChat(snapshot, auth);
  assert.match(html, /当前会话已达到上下文上限，请清空或新建会话/);
  assert.match(html, /class="enterprise-chat-send"[\s\S]*?disabled/);
  // 固定提示不能携带触发上限的草稿或既有对话正文，正文仍只允许后续 textContent 注入。
  assert.doesNotMatch(html, /count-limit-private|must-not-be-sent/);
});

test("enterprise Chat enforces UTF-8 history bytes and re-enables send after the draft is shortened", () => {
  const { state } = initializedChatState({ max_history_bytes: 12 });
  state.setDraft("abc");
  const firstTicket = state.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.ok(firstTicket);
  assert.equal(state.acceptMessage(firstTicket, {
    chat_session_id: "chs_1234567890abcdef1234567890abcdef",
    client_message_id: firstTicket.clientMessageId,
    assistant: { role: "assistant", content: "123456" },
    execution: { request_id: "req_byte_limit", trace_id: "trc_byte_limit" },
    session_revision: 2,
  }), true);

  // 既有历史为 9 字节，“中文”为 6 个 UTF-8 字节，必须在发请求前失败关闭并给出固定提示。
  state.setDraft("中文");
  assert.equal(state.snapshot().historyLimitReached, true);
  assert.equal(state.snapshot().historyLimitMessage, "当前会话已达到上下文上限，请清空或新建会话");
  assert.equal(state.beginSend("e4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0"), null);

  // 缩短到 3 字节后总历史恰为 12 字节，前端恢复发送；后端仍会独立执行相同合同校验。
  state.setDraft("xyz");
  assert.equal(state.snapshot().historyLimitReached, false);
  assert.equal(state.snapshot().historyLimitMessage, "");
  assert.ok(state.beginSend("f4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0"));
});

test("enterprise Chat plaintext never enters the HTML template and hydration uses textContent/value", () => {
  const { state, auth } = initializedChatState();
  const prompt = "<img src=x onerror=promptCanary()>";
  const answer = "<svg onload=responseCanary()>";
  state.setDraft(prompt);
  const ticket = state.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  state.acceptMessage(ticket, {
    chat_session_id: "chs_1234567890abcdef1234567890abcdef",
    chat_message_id: "chm_1234567890abcdef1234567890abcdef",
    client_message_id: ticket.clientMessageId,
    assistant: { role: "assistant", content: answer },
    execution: { request_id: "req_1", trace_id: "trc_1", total_tokens: 1, cost_cny: "0.000001" },
    session_revision: 2,
  });
  state.setDraft("next-draft-canary");
  const snapshot = state.snapshot();
  const html = renderEnterpriseChat(snapshot, auth);
  assert.doesNotMatch(html, /promptCanary|responseCanary|next-draft-canary|<img|<svg/);
  assert.match(html, /data-enterprise-chat-message-index="0"/);
  assert.doesNotMatch(html, /request_id|trace_id|enterprise-chat-governance/);

  const bodies = [{ textContent: "" }, { textContent: "" }];
  const elements = bodies.map((body, index) => ({
    getAttribute: () => String(index),
    querySelector: () => body,
  }));
  const textarea = { value: "" };
  const root = {
    querySelectorAll: () => elements,
    querySelector: () => textarea,
  };
  assert.equal(hydrateEnterpriseChatText(root, snapshot), true);
  assert.equal(bodies[0].textContent, prompt);
  assert.equal(bodies[1].textContent, answer);
  assert.equal(textarea.value, "next-draft-canary");
});

test("identity drift aborts in-flight send and destroys prior tenant plaintext", () => {
  const { state } = initializedChatState();
  state.setDraft("tenant-a-private-prompt");
  const ticket = state.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.ok(ticket);
  assert.equal(state.syncOwner(chatAuth({
    tenantId: "tenant_chat_b",
    userId: "usr_chat_b",
    sessionId: "ses_chat_b",
    fingerprint: "chat_fingerprint_b",
  }), "enterprise-chat"), true);
  assert.equal(ticket.signal.aborted, true);
  assert.equal(state.acceptMessage(ticket, { assistant: { content: "late-tenant-a-response" } }), false);
  assert.equal(state.snapshot().messages.length, 0);
  assert.equal(state.snapshot().draft, "");
  assert.equal(state.snapshot().session, null);
});

test("401 and 403 are strong plaintext boundaries while ordinary errors retain only fixed public fields", () => {
  const unauthorized = initializedChatState().state;
  unauthorized.setDraft("private-on-401");
  const unauthorizedTicket = unauthorized.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.equal(unauthorized.reject(unauthorizedTicket, new AuthApiError({
    status: 401,
    code: "auth.token_expired",
    message: "登录已过期",
    requestId: "req_401",
  })), true);
  assert.equal(unauthorized.snapshot().draft, "");
  assert.equal(unauthorized.snapshot().session, null);

  const failed = initializedChatState().state;
  failed.setDraft("private-not-in-error");
  const failedTicket = failed.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  failed.reject(failedTicket, new AuthApiError({
    status: 503,
    code: "chat.gateway_unavailable",
    message: "服务暂不可用",
    requestId: "req_503",
    traceId: "trc_503",
  }));
  assert.deepEqual(failed.snapshot().error, {
    status: 503,
    code: "chat.gateway_unavailable",
    message: "服务暂不可用",
    requestId: "req_503",
    traceId: "trc_503",
  });
  assert.doesNotMatch(JSON.stringify(failed.snapshot().error), /private-not-in-error/);
  assert.equal(failed.snapshot().status, "uncertain");
  failed.setDraft("must-not-resend-with-new-id");
  assert.equal(failed.beginSend("e4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0"), null);
  assert.ok(failed.beginClearSession());
});

test("expired or disabled resource responses discard the unusable session and allow a fresh create", () => {
  const { state } = initializedChatState();
  state.setDraft("private-expired-session-prompt");
  const ticket = state.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.equal(state.reject(ticket, new AuthApiError({
    status: 409,
    code: "chat.session_expired",
    message: "会话已过期",
    requestId: "req_expired",
    traceId: "trc_expired",
  })), true);
  const snapshot = state.snapshot();
  assert.equal(snapshot.session, null);
  assert.equal(snapshot.messages.length, 0);
  assert.equal(snapshot.draft, "");
  assert.equal(snapshot.options.projects.length > 0, true);
  assert.equal(snapshot.error.code, "chat.session_expired");
  assert.ok(state.beginCreateSession());
});

test("renderer fails closed without chat.use and responsive CSS keeps controls and long ids bounded", async () => {
  assert.equal(renderEnterpriseChat({}, chatAuth({ permissions: [] })), "");
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.enterprise-chat\s*\{[\s\S]*grid-template-rows:\s*auto auto minmax\(0, 1fr\) auto 20px/);
  assert.match(css, /\.enterprise-chat-icon-button,[\s\S]*width:\s*40px;[\s\S]*height:\s*40px/);
  assert.match(css, /\.enterprise-chat-message-body\s*\{[\s\S]*overflow-wrap:\s*anywhere/);
  assert.match(css, /@media \(max-width:\s*720px\)[\s\S]*\.enterprise-chat-selectors[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /\.enterprise-chat-selectors select[\s\S]*text-overflow:\s*ellipsis/);
});

test("authorized enterprise-chat navigation renders PAGE-093 through the existing console shell", () => {
  const { state, auth } = initializedChatState();
  state.setDraft("shell-private-draft-canary");
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: auth,
    enterpriseChatState: state.snapshot(),
    consoleShellState: {
      state: "ok",
      activeNavigationItemId: "enterprise-chat",
      navigation: {
        active_navigation_item_id: "enterprise-chat",
        navigation_items: [{
          id: "enterprise-chat",
          label: "企业 Chat",
          group: "workspace",
          target_section: "enterprise-chat",
          required_permissions: [PERMISSIONS.CHAT_USE],
          visible: true,
          disabled: false,
        }],
      },
    },
  });
  assert.match(container.innerHTML, /enterprise-chat-shell/);
  assert.match(container.innerHTML, /data-enterprise-chat-root/);
  // 草稿要由 main.js 在 DOM 就位后通过 value 注入，不能进入 Shell 的 innerHTML 字符串。
  assert.doesNotMatch(container.innerHTML, /shell-private-draft-canary/);
});

test("main lifecycle contains all plaintext cleanup and refresh gates without Storage or URL persistence", async () => {
  const [mainSource, stateSource, rendererSource, clientSource] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/enterprise-chat/enterpriseChatPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/enterprise-chat/renderEnterpriseChat.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/enterpriseChat.js", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /const session = sameWorkspaceConsoleSession\(authSession, nextSession\) \? authSession : nextSession;\s*const sessionChanged = authSession !== session;\s*if \(sessionChanged\) \{\s*clearConsoleSessionState\(\{ clearNavigation: false \}\)/);
  assert.match(mainSource, /function clearConsoleSessionState[\s\S]*clearEnterpriseChatSessionState\(\)/);
  assert.doesNotMatch(mainSource, /window\.setInterval\(\(\) => \{[\s\S]*?void refresh\(\);/);
  assert.match(mainSource, /case "enterprise-chat":[\s\S]*loadEnterpriseChatState\(consoleShellState\)/);
  assert.match(mainSource, /window\.addEventListener\("pagehide"[\s\S]*clearEnterpriseChatSessionState\(\)/);
  assert.match(mainSource, /function renderLogin[\s\S]*clearEnterpriseChatSessionState\(\)/);
  assert.match(mainSource, /logoutButton[\s\S]*clearEnterpriseChatSessionState\(\)/);
  assert.match(mainSource, /itemId !== "enterprise-chat"[\s\S]*clearEnterpriseChatSessionState\(\)/);
  assert.match(mainSource, /submit\.disabled[\s\S]*state\.historyLimitReached/);
  assert.match(mainSource, /liveStatus\.textContent\s*=\s*state\.historyLimitMessage\s*\?\?\s*""/);
  // 慢全页刷新结束时必须读取最新状态，避免用创建会话前的旧快照重新禁用消息输入框。
  assert.match(mainSource, /enterpriseChatState:\s*enterpriseChatPageState\.snapshot\(\)/);
  assert.match(rendererSource, /body\.textContent\s*=/);
  assert.match(rendererSource, /textarea\.value\s*=/);
  const implementation = `${stateSource}\n${rendererSource}\n${clientSource}`;
  assert.doesNotMatch(implementation, /localStorage|sessionStorage|indexedDB|caches\.open|console\.(?:log|error|warn)|location\.hash/);
  assert.doesNotMatch(clientSource, /retry|setTimeout|setInterval/i);
});

test("template Chat client sends the exact version-variable pair and never submits template id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return response({ payload: { request_id: "req_template_chat", trace_id: "trc_template_chat" } });
  };
  await sendEnterpriseChatMessage({
    controlApiUrl: "http://control.local",
    token: "session-token",
    chatSessionId: "chs_1234567890abcdef1234567890abcdef",
    clientMessageId: "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
    history: [],
    promptTemplateVersionId: "ptv_chat_customer_reply_v1",
    templateVariables: { customer_name: "张三" },
  });
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    client_message_id: "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
    history: [],
    prompt_template_version_id: "ptv_chat_customer_reply_v1",
    template_variables: { customer_name: "张三" },
  });
  assert.doesNotMatch(calls[0].options.body, /prompt_template_id"/);
  await assert.rejects(
    () => sendEnterpriseChatMessage({
      controlApiUrl: "http://control.local",
      token: "session-token",
      chatSessionId: "chs_1234567890abcdef1234567890abcdef",
      clientMessageId: "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
      history: [],
      promptTemplateVersionId: "ptv_chat_customer_reply_v1",
    }),
    (error) => error instanceof AuthApiError && error.code === "chat.request_invalid",
  );
  const unsafeVariables = Object.create(null);
  unsafeVariables.__proto__ = "must-not-change-object-prototype";
  await assert.rejects(
    () => sendEnterpriseChatMessage({
      controlApiUrl: "http://control.local",
      token: "session-token",
      chatSessionId: "chs_1234567890abcdef1234567890abcdef",
      clientMessageId: "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
      history: [],
      promptTemplateVersionId: "ptv_chat_customer_reply_v1",
      templateVariables: unsafeVariables,
    }),
    (error) => error instanceof AuthApiError && error.code === "chat.request_invalid",
  );
  assert.equal(Object.prototype.mustNotChangeObjectPrototype, undefined);
  assert.equal(calls.length, 1);
});

test("template Chat uses even history, exact variables and rendered_user for the next in-memory turn", () => {
  const auth = chatAuth({ permissions: [PERMISSIONS.CHAT_USE, PERMISSIONS.PROMPT_TEMPLATE_USE] });
  const state = createEnterpriseChatPageState();
  assert.match(enterpriseChatTemplateUseIdentitySnapshot(auth), /prompt_template\.use/);
  assert.equal(state.syncOwner(auth, "enterprise-chat"), true);
  const options = state.beginOptions();
  assert.equal(state.acceptOptions(options, optionsPayload()), true);
  assert.equal(state.selectPromptTemplate("ptv_chat_customer_reply_v1"), true);
  assert.equal(state.snapshot().selectedModelAliasId, "mal_chat_1");
  const create = state.beginCreateSession();
  assert.equal(state.acceptSession(create, {
    session: {
      chat_session_id: "chs_1234567890abcdef1234567890abcdef",
      project_id: "prj_chat_1",
      environment_id: "env_prod_1",
      model_alias_id: "mal_chat_1",
      state_revision: 1,
    },
  }), true);
  assert.equal(state.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0"), null);
  assert.equal(state.setTemplateVariable("customer_name", "张三"), true);
  const send = state.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.deepEqual(send.history, []);
  assert.equal(send.promptTemplateVersionId, "ptv_chat_customer_reply_v1");
  assert.equal(Object.getPrototypeOf(send.templateVariables), null);
  assert.deepEqual({ ...send.templateVariables }, { customer_name: "张三" });
  assert.equal(state.acceptMessage(send, {
    chat_session_id: "chs_1234567890abcdef1234567890abcdef",
    client_message_id: send.clientMessageId,
    session_revision: 2,
    rendered_user: "请为客户张三生成回复",
    assistant: { role: "assistant", content: "您好，张三" },
    execution: { request_id: "req_template_chat", total_tokens: 8 },
  }), true);
  const snapshot = state.snapshot();
  assert.deepEqual(snapshot.messages.map((item) => item.content), ["请为客户张三生成回复", "您好，张三"]);
  assert.equal(Object.getPrototypeOf(snapshot.templateVariableValues), null);
  assert.deepEqual(Object.keys(snapshot.templateVariableValues), []);
});

test("template stale errors never silently downgrade to ordinary Chat while variables_invalid stays correctable", () => {
  const auth = chatAuth({ permissions: [PERMISSIONS.CHAT_USE, PERMISSIONS.PROMPT_TEMPLATE_USE] });
  // 每个错误分支使用全新 Session/状态机，避免上一分支的 stale 或变量值污染下一断言。
  const build = () => {
    const state = createEnterpriseChatPageState();
    state.syncOwner(auth, "enterprise-chat");
    state.acceptOptions(state.beginOptions(), optionsPayload());
    state.selectPromptTemplate("ptv_chat_customer_reply_v1");
    const create = state.beginCreateSession();
    state.acceptSession(create, {
      session: {
        chat_session_id: "chs_1234567890abcdef1234567890abcdef",
        project_id: "prj_chat_1",
        environment_id: "env_prod_1",
        model_alias_id: "mal_chat_1",
        state_revision: 1,
      },
    });
    state.setTemplateVariable("customer_name", "可修正值");
    return state;
  };
  const stale = build();
  const staleTicket = stale.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.equal(stale.reject(staleTicket, {
    status: 409,
    code: "prompt_template.version_conflict",
    message: "stale",
  }), true);
  assert.equal(stale.snapshot().templateOptionsStale, true);
  assert.equal(stale.snapshot().selectedPromptTemplateVersionId, "ptv_chat_customer_reply_v1");
  assert.equal(Object.getPrototypeOf(stale.snapshot().templateVariableValues), null);
  assert.deepEqual(Object.keys(stale.snapshot().templateVariableValues), []);
  stale.setDraft("must-not-be-sent-as-fallback");
  assert.equal(stale.beginSend("ebd54e76-cd2b-4825-82c9-67bb5fe54628"), null);
  assert.equal(stale.selectPromptTemplate(""), true);
  assert.ok(stale.beginSend("ebd54e76-cd2b-4825-82c9-67bb5fe54628"));

  const correctable = build();
  const correctableTicket = correctable.beginSend("d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0");
  assert.equal(correctable.reject(correctableTicket, {
    status: 422,
    code: "prompt_template.variables_invalid",
    message: "invalid",
  }), true);
  assert.equal(correctable.snapshot().templateOptionsStale, false);
  assert.equal(correctable.snapshot().templateVariableValues.customer_name, "可修正值");
  assert.equal(correctable.snapshot().status, "ready");
});

test("template variable values stay out of renderer HTML and recommendation does not mutate selected alias", () => {
  const auth = chatAuth({ permissions: [PERMISSIONS.CHAT_USE, PERMISSIONS.PROMPT_TEMPLATE_USE] });
  const state = createEnterpriseChatPageState();
  state.syncOwner(auth, "enterprise-chat");
  state.acceptOptions(state.beginOptions(), optionsPayload());
  state.selectPromptTemplate("ptv_chat_customer_reply_v1");
  state.setTemplateVariable("customer_name", "renderer-private-variable-canary");
  const before = state.snapshot().selectedModelAliasId;
  const html = renderEnterpriseChat(state.snapshot(), auth);
  assert.match(html, /data-enterprise-chat-template/);
  assert.match(html, /data-enterprise-chat-template-variable="customer_name"/);
  assert.match(html, /企业问答模型 · 推荐/);
  assert.doesNotMatch(html, /renderer-private-variable-canary/);
  assert.equal(state.snapshot().selectedModelAliasId, before);

  const ordinaryAuth = chatAuth({ permissions: [PERMISSIONS.CHAT_USE] });
  const ordinary = createEnterpriseChatPageState();
  ordinary.syncOwner(ordinaryAuth, "enterprise-chat");
  ordinary.acceptOptions(ordinary.beginOptions(), optionsPayload());
  assert.deepEqual(ordinary.snapshot().options.prompt_templates, []);
  assert.doesNotMatch(renderEnterpriseChat(ordinary.snapshot(), ordinaryAuth), /data-enterprise-chat-template/);

  const malformedTemplatePayload = optionsPayload();
  malformedTemplatePayload.prompt_templates[0].project_scope = "unexpected";
  const failClosed = createEnterpriseChatPageState();
  failClosed.syncOwner(auth, "enterprise-chat");
  assert.equal(failClosed.acceptOptions(failClosed.beginOptions(), malformedTemplatePayload), true);
  assert.deepEqual(failClosed.snapshot().options.prompt_templates, []);
  // 模板单项损坏只关闭模板目录，普通 Chat 的项目、模型和建会话主流程仍保持可用。
  assert.ok(failClosed.beginCreateSession());

  const reservedNamePayload = optionsPayload();
  reservedNamePayload.prompt_templates[0].variables[0].name = "__proto__";
  const reservedNameState = createEnterpriseChatPageState();
  reservedNameState.syncOwner(auth, "enterprise-chat");
  reservedNameState.acceptOptions(reservedNameState.beginOptions(), reservedNamePayload);
  assert.deepEqual(reservedNameState.snapshot().options.prompt_templates, []);
  assert.equal(Object.prototype.rendererPrivateVariableCanary, undefined);
});
