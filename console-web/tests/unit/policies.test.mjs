// 本单测覆盖 PRD-044 策略控制台，验证 API 合同、admin 门禁、内存草稿、异步 owner、全转义和响应式结构。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createEmptyPolicyScope,
  createPolicyDraftMemory,
  createPolicyRequestLifecycle,
  policyIdentityFromAuthContext,
  policyDraftFromPolicy,
  policyDraftSnapshot,
  renderPoliciesPanel,
  shouldLoadPolicies,
} from "../../src/features/policies/renderPoliciesPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import {
  createPolicy,
  listPolicies,
  updatePolicy,
  validatePolicy,
} from "../../src/lib/policies.js";

// 策略、模型和校验样本覆盖嵌套 rule/action、ETag、route_to 与用户可控文本，避免测试只验证空壳 HTML。
const activeModel = {
  model_id: "model_active",
  internal_model_id: "deepseek-chat",
  display_name: "DeepSeek <Active>",
  provider_code: "deepseek",
  status: "active",
};
const disabledModel = {
  model_id: "model_disabled",
  internal_model_id: "disabled-model",
  display_name: "Disabled Model",
  provider_code: "local",
  status: "disabled",
};
const policy = {
  policy_id: "pol_11111111111111111111111111111111",
  tenant_id: "tenant_must_not_render",
  name: "生产低预算策略",
  description: "接近预算上限时切换模型",
  status: "active",
  priority: 100,
  version: 3,
  dsl_version: "policy-dsl-v1",
  rule: {
    policy_rule_id: "prule_1",
    condition_expr: "env == \"prod\" and budget_remaining < 100",
    condition_hash: "a".repeat(64),
    variables: ["env", "budget_remaining"],
  },
  action: {
    policy_action_id: "pact_1",
    type: "route_to",
    target_model_id: "model_active",
  },
  updated_at: "2026-07-17T08:00:00Z",
};

// 权限夹具模拟后端认证快照；角色名仅用于身份展示，不能单独打开策略页面。
const policyWriterContext = {
  user: { role: "admin" },
  permission_codes: [PERMISSIONS.POLICY_READ, PERMISSIONS.POLICY_WRITE],
};
const policyReaderContext = {
  user: { role: "security_admin" },
  permission_codes: [PERMISSIONS.POLICY_READ],
};

test("policy client sends nested safe payloads and If-Match without tenant or compiled fields", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    const requestUrl = String(url);
    const payload = requestUrl.endsWith("/validate")
      ? {
        valid: true,
        dsl_version: "policy-dsl-v1",
        condition_hash: "a".repeat(64),
        variables: ["env"],
        canonical_expression: 'env == "prod"',
        request_id: "req_validate",
      }
      : options.method === "POST" || options.method === "PATCH"
        ? { policy: { ...policy, version: options.method === "PATCH" ? 4 : 1 }, request_id: "req_write" }
        : { policies: [policy], request_id: "req_list", trace_id: "trc_list" };
    return {
      ok: true,
      status: options.method === "POST" && !requestUrl.endsWith("/validate") ? 201 : 200,
      headers: new Map([
        ["ETag", options.method === "PATCH" ? '"4"' : '"1"'],
        ["X-Request-ID", "req_header"],
      ]),
      async json() {
        return payload;
      },
    };
  };

  const controller = new AbortController();
  const listed = await listPolicies({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    status: "active",
    action: "route_to",
    signal: controller.signal,
    tenantId: "tenant_must_not_send",
  });
  const validated = await validatePolicy({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    conditionExpr: 'env == "prod"',
    actionType: "deny",
    targetModelId: "model_must_be_cleared",
  });
  await createPolicy({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    name: "生产路由",
    description: "",
    status: "active",
    priority: "100",
    conditionExpr: 'env == "prod"',
    actionType: "route_to",
    targetModelId: "model_active",
  });
  const updated = await updatePolicy({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    policyId: policy.policy_id,
    version: 3,
    name: "生产阻断",
    description: "预算耗尽",
    status: "disabled",
    priority: "90",
    conditionExpr: "budget_remaining < 10",
    actionType: "deny",
    targetModelId: "model_must_be_cleared",
  });

  assert.equal(listed.state, "ok");
  assert.equal(validated.valid, true);
  assert.equal(updated.etagVersion, 4);
  assert.equal(calls[0].options.signal, controller.signal);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.match(calls[0].url, /status=active/);
  assert.match(calls[0].url, /action=route_to/);
  assert.doesNotMatch(calls[0].url, /tenant/);

  const validateBody = JSON.parse(calls[1].options.body);
  assert.deepEqual(validateBody, {
    rule: { condition_expr: 'env == "prod"' },
    action: { type: "deny", target_model_id: null },
  });
  const createBody = JSON.parse(calls[2].options.body);
  assert.deepEqual(createBody.action, { type: "route_to", target_model_id: "model_active" });
  assert.equal(createBody.description, null);
  assert.equal(createBody.priority, 100);
  const patchBody = JSON.parse(calls[3].options.body);
  assert.equal(calls[3].options.method, "PATCH");
  assert.equal(calls[3].options.headers["If-Match"], '"3"');
  assert.match(calls[3].url, new RegExp(`/policies/${policy.policy_id}$`));
  assert.deepEqual(patchBody.action, { type: "deny", target_model_id: null });
  for (const body of [validateBody, createBody, patchBody]) {
    assert.equal(body.tenant_id, undefined);
    assert.equal(body.version, undefined);
    assert.equal(body.condition_ast, undefined);
    assert.equal(body.condition_hash, undefined);
  }
});

test("policy client uses Workspace cookies and CSRF for reads and writes", async () => {
  const previousDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=csrf_test_value" };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      headers: { get() { return null; } },
      async json() { return { policies: [], valid: true }; },
    };
  };
  try {
    await listPolicies({ controlApiUrl: "http://control.local", authentication: "workspace" });
    await validatePolicy({
      controlApiUrl: "http://control.local", authentication: "workspace",
      conditionExpr: 'env == "prod"', actionType: "deny",
    });
    assert.equal(calls.length, 2);
    assert.equal(calls[0].options.credentials, "include");
    assert.equal(calls[0].options.headers.Authorization, undefined);
    assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
    assert.equal(calls[1].options.credentials, "include");
    assert.equal(calls[1].options.headers.Authorization, undefined);
    assert.equal(calls[1].options.headers["X-CSRF-Token"], "csrf_test_value");
  } finally {
    globalThis.document = previousDocument;
  }
});

test("policy client maps unified errors without reflecting DSL source", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "policy.expression_invalid",
        message: "策略表达式无效",
        request_id: "req_invalid",
        trace_id: "trc_invalid",
      };
    },
  });

  await assert.rejects(
    () => createPolicy({
      controlApiUrl: "http://control.local",
      token: "jwt-token",
      conditionExpr: '__import__("os")',
      actionType: "deny",
    }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "policy.expression_invalid");
      assert.equal(error.requestId, "req_invalid");
      assert.equal(error.traceId, "trc_invalid");
      assert.doesNotMatch(error.message, /__import__/);
      return true;
    },
  );
});

test("policy panel renders admin editor active models and escaped dynamic values", () => {
  const unsafePolicy = {
    ...policy,
    name: '<img src=x onerror="alert(1)">',
    description: '" autofocus onfocus="alert(1)',
    rule: { ...policy.rule, condition_expr: "</textarea><script>alert(1)</script>" },
  };
  const draft = policyDraftFromPolicy(unsafePolicy);
  const snapshot = policyDraftSnapshot(draft, { selectedPolicyId: unsafePolicy.policy_id });
  const html = renderPoliciesPanel(
    {
      state: "ok",
      policies: [unsafePolicy],
      filters: { status: "all", action: "all" },
      editor: {
        selectedPolicyId: unsafePolicy.policy_id,
        draft,
        dirty: true,
        currentSnapshot: snapshot,
        operationStatus: "idle",
        canSave: true,
        validation: {
          valid: true,
          dsl_version: "policy-dsl-v1",
          variables: ["env", "<unsafe-variable>"],
          condition_hash: "f".repeat(64),
          canonical_expression: '<script>canonical</script>',
          request_id: 'req_"<unsafe>',
        },
      },
    },
    { state: "ok", models: [activeModel, disabledModel] },
    policyWriterContext,
  );

  assert.match(html, /id="policy-editor-form"/);
  assert.match(html, /data-policy-route-target-field/);
  assert.match(html, /value="model_active" selected/);
  assert.match(html, /DeepSeek &lt;Active&gt;/);
  assert.doesNotMatch(html, /model_disabled/);
  assert.doesNotMatch(html, /Disabled Model/);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.match(html, /&lt;\/textarea&gt;&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /canonical_expression|&lt;script&gt;canonical&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /tenant_must_not_render/);
  assert.match(html, /data-policy-save type="submit" >/);
  assert.doesNotMatch(html, /data-policy-save type="submit" disabled/);
});

test("policy diagnostics and all non-admin renders remain safe", () => {
  const scope = createEmptyPolicyScope();
  scope.draft.conditionExpr = "x".repeat(1024);
  const html = renderPoliciesPanel(
    {
      state: "ok",
      policies: [],
      filters: { status: "all", action: "all" },
      editor: {
        ...scope,
        currentSnapshot: policyDraftSnapshot(scope.draft),
        operationStatus: "idle",
        canSave: false,
        validation: {
          valid: false,
          diagnostics: [
            {
              code: "policy.expression_invalid",
              field: 'rule.condition_expr"><img src=x>',
              start: 0,
              end: 7,
              message: "<svg onload=alert(1)>",
            },
          ],
          request_id: "req_</code><script>",
        },
      },
    },
    { state: "ok", models: [activeModel] },
    policyWriterContext,
  );

  assert.match(html, /&lt;svg onload=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /req_&lt;\/code&gt;&lt;script&gt;|rule\.condition_expr/);
  assert.match(html, /data-policy-save type="submit" disabled/);
  const readOnly = renderPoliciesPanel(
    { state: "ok", policies: [policy], filters: { status: "all", action: "all" } },
    { state: "ok", models: [activeModel] },
    policyReaderContext,
  );
  assert.match(readOnly, /生产低预算策略/);
  assert.doesNotMatch(readOnly, /policy-editor-form|data-policy-edit|policy-new/);
  assert.equal(renderPoliciesPanel({ state: "ok", policies: [policy] }, null, { user: { role: "developer" } }), "");
  assert.equal(renderPoliciesPanel({ state: "ok", policies: [policy] }, null, { user: { role: "viewer" } }), "");
});

test("policy overview keeps the advanced editor closed until the user starts a rule", () => {
  const base = { state: "ok", policies: [], filters: { status: "all", action: "all" } };
  const overview = renderPoliciesPanel({ ...base, editor: createEmptyPolicyScope() }, null, policyWriterContext);
  assert.match(overview, /新建自定义规则/);
  assert.match(overview, /尚无自定义规则/);
  assert.doesNotMatch(overview, /id="policy-editor-form"/);
  const started = renderPoliciesPanel({ ...base, editor: { ...createEmptyPolicyScope(), open: true } }, null, policyWriterContext);
  assert.match(started, /id="policy-editor-form"/);
  assert.doesNotMatch(started, /尚无自定义规则/);
  assert.match(started, /1\. 校验规则/);
  assert.match(started, /2\. 保存草稿/);
  assert.match(started, /data-policy-variable="tenant"/);
  assert.match(started, /data-policy-variable="project"/);
});

test("Workspace owner supplies isolated rule editor identity", () => {
  const identity = policyIdentityFromAuthContext(
    { permission_codes: [PERMISSIONS.POLICY_READ] },
    { workspaceId: "wsp_test", globalSessionId: "session_test", role: "owner" },
  );
  assert.deepEqual(identity, { tenantId: "wsp_test", userId: "session_test", role: "owner" });
});

test("policy draft memory retains same identity refresh and clears tenant user or role changes", () => {
  const memory = createPolicyDraftMemory();
  const admin = { tenantId: "tenant_1", userId: "user_1", role: "admin" };
  const initial = memory.enter(admin);
  const dirtyScope = {
    ...initial,
    draft: { ...initial.draft, conditionExpr: 'env == "prod"' },
    dirty: true,
  };
  memory.write(admin, dirtyScope);

  assert.equal(memory.enter({ ...admin }).dirty, true);
  assert.equal(memory.read(admin).draft.conditionExpr, 'env == "prod"');
  const roleChanged = memory.enter({ ...admin, role: "developer" });
  assert.equal(roleChanged.dirty, false);
  assert.equal(roleChanged.draft.conditionExpr, "");
  const tenantChanged = memory.enter({ tenantId: "tenant_2", userId: "user_1", role: "admin" });
  assert.equal(tenantChanged.draft.conditionExpr, "");
  memory.clear();
  assert.equal(memory.read({ tenantId: "tenant_2", userId: "user_1", role: "admin" }), null);
});

test("policy snapshot changes for every editable field object and version", () => {
  const draft = policyDraftFromPolicy(policy);
  const base = policyDraftSnapshot(draft, { selectedPolicyId: policy.policy_id });
  const variants = [
    { ...draft, name: `${draft.name}-changed` },
    { ...draft, description: `${draft.description}-changed` },
    { ...draft, status: "disabled" },
    { ...draft, priority: "101" },
    { ...draft, conditionExpr: 'env == "test"' },
    { ...draft, actionType: "deny", targetModelId: null },
    { ...draft, targetModelId: "model_other" },
    { ...draft, version: 4 },
  ];
  for (const variant of variants) {
    assert.notEqual(policyDraftSnapshot(variant, { selectedPolicyId: policy.policy_id }), base);
  }
  assert.notEqual(policyDraftSnapshot(draft, { selectedPolicyId: "pol_other" }), base);
});

test("policy saving state locks fields filters and policy switching", () => {
  const draft = policyDraftFromPolicy(policy);
  const html = renderPoliciesPanel(
    {
      state: "ok",
      policies: [policy],
      filters: { status: "all", action: "all" },
      editor: {
        selectedPolicyId: policy.policy_id,
        draft,
        dirty: true,
        operationStatus: "saving",
        canSave: false,
        validation: null,
      },
    },
    { state: "ok", models: [activeModel] },
    policyWriterContext,
  );

  assert.match(html, /id="policy-new" type="button" disabled/);
  assert.match(html, /name="name"[^>]*disabled/);
  assert.match(html, /name="conditionExpr"[^>]*disabled/);
  assert.match(html, /name="policyStatus" disabled/);
  assert.match(html, /data-policy-edit="pol_[^"]+" type="button" disabled/);
});

test("policy lifecycle rejects out of order snapshot identity navigation and refresh responses", () => {
  const lifecycle = createPolicyRequestLifecycle();
  const first = lifecycle.begin({
    kind: "validate",
    snapshot: "snapshot-a",
    identity: "tenant-a-user-a-admin",
    navigation: "policies",
    generation: 10,
  });
  const second = lifecycle.begin({
    kind: "validate",
    snapshot: "snapshot-b",
    identity: "tenant-a-user-a-admin",
    navigation: "policies",
    generation: 10,
  });

  assert.equal(first.signal.aborted, true);
  assert.equal(lifecycle.isCurrent(first, {
    snapshot: "snapshot-a",
    identity: "tenant-a-user-a-admin",
    navigation: "policies",
    generation: 10,
  }), false);
  assert.equal(lifecycle.isCurrent(second, {
    snapshot: "snapshot-b",
    identity: "tenant-a-user-a-admin",
    navigation: "policies",
    generation: 10,
  }), true);
  for (const current of [
    { snapshot: "changed", identity: "tenant-a-user-a-admin", navigation: "policies", generation: 10 },
    { snapshot: "snapshot-b", identity: "tenant-b-user-a-admin", navigation: "policies", generation: 10 },
    { snapshot: "snapshot-b", identity: "tenant-a-user-a-admin", navigation: "overview", generation: 10 },
    { snapshot: "snapshot-b", identity: "tenant-a-user-a-admin", navigation: "policies", generation: 11 },
  ]) {
    assert.equal(lifecycle.isCurrent(second, current), false);
  }
  assert.equal(lifecycle.isBusy(), true);
  lifecycle.invalidate();
  assert.equal(second.signal.aborted, true);
  assert.equal(lifecycle.isBusy(), false);
});

test("policy active loader predicate requires read permission navigation hash generation and identity", () => {
  const valid = {
    canRead: true,
    activeNavigationItemId: "policies",
    hashNavigation: "policies",
    refreshId: 12,
    currentRefreshId: 12,
    requestIdentity: "tenant-user-admin",
    currentIdentity: "tenant-user-admin",
  };
  assert.equal(shouldLoadPolicies(valid), true);
  assert.equal(shouldLoadPolicies({ ...valid, hashNavigation: undefined }), true);
  assert.equal(shouldLoadPolicies({ ...valid, canRead: false }), false);
  assert.equal(shouldLoadPolicies({ ...valid, activeNavigationItemId: "overview" }), false);
  assert.equal(shouldLoadPolicies({ ...valid, hashNavigation: "dlp-rules" }), false);
  assert.equal(shouldLoadPolicies({ ...valid, currentRefreshId: 13 }), false);
  assert.equal(shouldLoadPolicies({ ...valid, currentIdentity: "other-user" }), false);
});

test("developer start renders policies only for an active permission snapshot", () => {
  const shellState = {
    activeNavigationItemId: "policies",
    navigation: {
      active_navigation_item_id: "policies",
      navigation_items: [
        {
          id: "policies",
          label: "策略管理",
          group: "安全与治理",
          target_section: "policies",
          description: "策略管理",
          order: 87,
        },
      ],
    },
  };
  const adminContainer = { innerHTML: "" };
  const developerContainer = { innerHTML: "" };
  renderDeveloperStart(adminContainer, {
    authContext: {
      user: { role: "admin", display_name: "Admin", email: "admin@example.com", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-17T12:00:00Z" },
      permission_codes: [PERMISSIONS.POLICY_READ, PERMISSIONS.POLICY_WRITE],
    },
    consoleShellState: shellState,
    policiesState: {
      state: "ok",
      policies: [policy],
      editor: { ...createEmptyPolicyScope(), operationStatus: "idle", canSave: false },
    },
    modelsState: { state: "ok", models: [activeModel] },
  });
  renderDeveloperStart(developerContainer, {
    authContext: {
      user: { role: "developer", display_name: "Dev", email: "dev@example.com", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-17T12:00:00Z" },
      permission_codes: [],
    },
    consoleShellState: shellState,
    policiesState: { state: "ok", policies: [policy] },
    modelsState: { state: "ok", models: [activeModel] },
  });

  assert.match(adminContainer.innerHTML, /class="panel policies-panel"/);
  assert.match(adminContainer.innerHTML, /生产低预算策略/);
  assert.doesNotMatch(developerContainer.innerHTML, /policies-panel/);
  assert.doesNotMatch(developerContainer.innerHTML, /生产低预算策略/);
});

test("main integration and responsive CSS keep active-only no-storage boundaries", async () => {
  const [mainSource, featureSource, clientSource, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/policies/renderPoliciesPanel.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/policies.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);

  assert.match(mainSource, /loadPoliciesState\(consoleShellState, refreshId, modelsState\)/);
  assert.match(mainSource, /shouldLoadPolicies\(\{/);
  assert.match(mainSource, /policyListController\?\.abort\(\)/);
  assert.match(mainSource, /policyDraftSnapshot\(scope\.draft/);
  assert.match(mainSource, /policyRequestLifecycle\.invalidate\(\)/);
  assert.match(mainSource, /consoleShellState\?\.activeNavigationItemId !== "policies"/);
  assert.doesNotMatch(featureSource, /(?:localStorage|sessionStorage)\s*\./);
  assert.doesNotMatch(clientSource, /tenant_id\s*:/);
  assert.doesNotMatch(featureSource, /data-[^=]+="\$\{escapeAttribute\(JSON/);
  assert.match(css, /\.policy-condition code \{[\s\S]*?overflow: auto;/);
  assert.match(css, /@media \(max-width: 1100px\)[\s\S]*?\.policy-row:not\(\.policy-row-head\)/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?#policy-editor-form,[\s\S]*?grid-template-columns: minmax\(0, 1fr\);/);
});
