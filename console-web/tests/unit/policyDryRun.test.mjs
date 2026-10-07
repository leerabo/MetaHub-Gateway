// PRD-046 前端专项：验证 admin 门禁、Prompt 短生命周期、统一 API、竞态和响应式安全渲染。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createEmptyPolicyDryRunState,
  createPolicyDryRunLifecycle,
  policyDryRunIdentitySnapshot,
  renderPolicyDryRunPanel,
  shouldOwnPolicyDryRun,
} from "../../src/features/policy-dry-run/renderPolicyDryRunPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import {
  createPolicyDryRunRequestId,
  executePolicyDryRun,
} from "../../src/lib/policyDryRun.js";
import { withPermissionSnapshot } from "./permissionTestContext.mjs";

const promptSentinel = "PRD046_PROMPT_SENTINEL_NEVER_RENDER";
const authContext = withPermissionSnapshot({
  user: { tenant_id: "tenant_1", user_id: "user_1", role: "admin", email: "admin@example.com" },
  tenant: { tenant_id: "tenant_1", name: "默认租户" },
  session: { session_id: "session_1" },
}, [PERMISSIONS.POLICY_SIMULATE]);
const contextState = {
  environmentState: {
    state: "ok",
    environments: [{ environment_id: "env_1", code: "prod", name: "生产", status: "active" }],
  },
  projectState: {
    state: "ok",
    projects: [{ project_id: "prj_1", project_code: "core", name: "核心项目", status: "active" }],
  },
  modelAliasesState: {
    state: "ok",
    model_aliases: [{ alias: "chat-default", environment_id: "env_1", environment: "prod", status: "active" }],
  },
  modelsState: {
    state: "ok",
    models: [{ model_id: "mdl_1", model_alias: "direct-chat", status: "active" }],
  },
};
const explanationResponse = {
  request_id: "req_dry_run_1",
  trace_id: "trc_dry_run_1",
  policy_explanation: {
    policy_explanation_id: "pexp_11111111111111111111111111111111",
    policy_source: "active_candidate",
    published_snapshot_version: 10,
    runtime_compatible: false,
    runtime_incompatibility_codes: ["policy_snapshot.variable_unsupported"],
    gateway_parity: "partial",
    coverage_complete: false,
    outcome: "would_allow",
    would_error_code: null,
    coverage: {
      policy: true,
      model_provider_routing: true,
      chat_capability: true,
      dlp: true,
      tenant_project_budget: true,
      virtual_key_application_budget: false,
      rpm_tpm_concurrency_limits: false,
      provider_credentials_keypool: false,
      real_provider: false,
      not_simulated: [
        "application_budget",
        "virtual_key_budget",
        "rpm_tpm_concurrency_limits",
        "provider_credentials_keypool",
        "real_provider_call",
      ],
    },
    decision_tree: {
      nodes: [
        { node_id: "node_001", parent_id: "node_000", order: 1, node_type: "policy_rule", status: "matched", priority: 10, action: "allow", reason_code: "policy.rule_matched" },
        { node_id: "node_000", parent_id: null, order: 0, node_type: "policy_set", status: "completed", reason_code: "policy.rule_matched" },
        { node_id: "node_002", parent_id: "node_000", order: 2, node_type: "route", status: "selected", reason_code: "alias.default" },
      ],
    },
    policy: {
      decision: "allow",
      policy_checks: [
        { policy_rule_id: "prule_1", priority: 10, action: "allow", matched: true, match_order: 1 },
      ],
    },
    route: {
      status: "selected",
      selected_model_id: "mdl_1",
      internal_model_id: "direct-chat",
      provider_code: "deepseek",
      routing_policy_id: "rpol_1",
      routing_policy_version: 7,
      routing_mode: "cost_first",
      price_as_of: "2026-07-18T09:00:00Z",
      estimated_input_tokens: 12,
      estimated_output_tokens: 512,
      token_estimate_truncated: false,
      chat_capability_supported: true,
      provider_credential_status: "unknown",
      selection_reason: "routing.cost_lowest",
      candidates: [
        {
          role: "default",
          rank: 1,
          model_id: "mdl_1",
          internal_model_id: "direct-chat",
          provider_code: "deepseek",
          eligible: true,
          status: "selected",
          health_state: "healthy",
          health_fresh: true,
          keypool_state: "available",
          estimated_cost_amount: "0.000000000000000",
          currency: "CNY",
          reason_code: "routing.cost_lowest",
        },
        {
          role: "fallback",
          model_id: "mdl_2",
          internal_model_id: "fallback-chat",
          provider_code: "zhipu",
          eligible: false,
          status: "rejected",
          health_state: "unhealthy",
          health_fresh: false,
          keypool_state: "unavailable",
          estimated_cost_amount: null,
          currency: null,
          price_source: "missing",
          reason_code: "routing.candidate.provider_unhealthy",
        },
      ],
    },
    dlp: {
      status: "matched",
      match_count: 1,
      matches: [{ dlp_rule_id: "dlp_rule_1", priority: 20, action: "log_only" }],
    },
    budget: {
      status: "within_limit",
      estimated_input_tokens: 12,
      estimated_output_tokens: 512,
      estimated_cost_cny: "0.001200",
      rules: [{
        budget_rule_id: "budget_1",
        scope_type: "tenant",
        status: "within_limit",
        action: "allow",
        projected_cny: "2.001200",
        limit_cny: "10.000000",
      }],
    },
    // 即使恶意或损坏响应夹带这些未声明字段，renderer 也不能读取或展示。
    prompt: promptSentinel,
    input_summary: { prompt_preview: promptSentinel },
  },
};

test("dry-run client sends bounded POST contract without tenant in URL or payload", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      headers: new Map([["X-Request-ID", "req_header"]]),
      async json() { return explanationResponse; },
    };
  };
  const controller = new AbortController();
  const result = await executePolicyDryRun({
    controlApiUrl: "http://control.local",
    token: "session-token",
    prompt: promptSentinel,
    model: "chat-default",
    projectId: "prj_1",
    environmentId: "env_1",
    dataClass: "confidential",
    maxOutputTokens: 512,
    requestId: "req_stable_dry_run",
    signal: controller.signal,
  });
  assert.equal(result.policy_explanation.policy_source, "active_candidate");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://control.local/policies/dry-run");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.signal, controller.signal);
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_stable_dry_run");
  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.prompt, promptSentinel);
  assert.equal(body.environment_id, "env_1");
  assert.equal(body.project_id, "prj_1");
  assert.equal(body.tenant_id, undefined);
  assert.doesNotMatch(calls[0].url, /tenant|prompt|project/i);
  assert.match(createPolicyDryRunRequestId(), /^req_console_policy_dry_run_/);
});

test("dry-run client maps unified errors without attaching request payload", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Map(),
    async json() {
      return {
        code: "policy.dry_run_payload_invalid",
        message: "请求字段不合法",
        request_id: "req_invalid",
        trace_id: "trc_invalid",
      };
    },
  });
  await assert.rejects(
    () => executePolicyDryRun({
      controlApiUrl: "http://control.local",
      token: "session-token",
      prompt: promptSentinel,
      model: "chat-default",
      projectId: "prj_1",
      environmentId: "env_1",
      dataClass: "confidential",
    }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "policy.dry_run_payload_invalid");
      assert.equal(error.requestId, "req_invalid");
      assert.doesNotMatch(JSON.stringify(error), new RegExp(promptSentinel));
      return true;
    },
  );
});

test("renderer separates publication compatibility from partial coverage and never restores prompt", () => {
  const state = {
    ...createEmptyPolicyDryRunState(),
    status: "success",
    environmentId: "env_1",
    projectId: "prj_1",
    model: "chat-default",
    result: explanationResponse,
  };
  const html = renderPolicyDryRunPanel(state, contextState, authContext);
  assert.match(html, /策略模拟器/);
  assert.match(html, /当前发布合同不兼容/);
  assert.match(html, /部分覆盖/);
  assert.match(html, /policy_snapshot.variable_unsupported/);
  assert.match(html, /prule_1/);
  assert.match(html, /P10/);
  assert.match(html, /data-depth="1"/);
  assert.match(html, /policy_set[\s\S]*policy_rule[\s\S]*route/);
  assert.match(html, /data-label="顺序">1/);
  assert.match(html, /Chat 能力[\s\S]*支持/);
  assert.match(html, /cost_first/);
  assert.match(html, /rpol_1/);
  assert.match(html, /v7/);
  assert.match(html, /2026-07-18T09:00:00Z/);
  assert.match(html, /routing\.cost_lowest/);
  assert.match(html, /0\.000000000000000 CNY/);
  assert.match(html, /备用角色（初始候选）/);
  assert.match(html, /available/);
  assert.match(html, /unavailable/);
  assert.match(html, /healthy \/ fresh/);
  assert.match(html, /unhealthy \/ stale/);
  assert.match(html, /缺价/);
  assert.match(html, /0\.001200/);
  assert.match(html, /pexp_11111111111111111111111111111111/);
  assert.match(html, /已发布版本[\s\S]*10/);
  assert.match(html, /模型与 Provider 配置路由/);
  assert.match(html, /Provider 凭据与 KeyPool/);
  assert.match(html, /routing\.candidate\.provider_unhealthy/);
  assert.match(html, /dlp_rule_1/);
  assert.match(html, /2\.001200[\s\S]*10\.000000/);
  assert.match(html, /<textarea[^>]*name="prompt"[^>]*><\/textarea>/);
  assert.doesNotMatch(html, new RegExp(promptSentinel));
  // 未覆盖范围允许展示 provider_keypool 这一能力名称，但不得泄漏同名敏感字段本身。
  assert.doesNotMatch(html, /\b(?:provider_key|base_url|authorization|condition_expr)\b/i);
  assert.equal(Object.hasOwn(createEmptyPolicyDryRunState(), "prompt"), false);
  assert.equal(renderPolicyDryRunPanel(
    state,
    contextState,
    withPermissionSnapshot({ user: { role: "developer" } }, []),
  ), "");
  assert.equal(renderPolicyDryRunPanel(
    state,
    contextState,
    withPermissionSnapshot({ user: { role: "viewer" } }, []),
  ), "");
});

test("renderer collapses the form until environment, project and model setup exists", () => {
  const html = renderPolicyDryRunPanel(
    createEmptyPolicyDryRunState(),
    {
      environmentState: { state: "ok", environments: [] },
      projectState: { state: "ok", projects: [] },
      modelAliasesState: { state: "ok", model_aliases: [] },
      modelsState: { state: "ok", models: [] },
    },
    authContext,
  );
  assert.match(html, /完成基础配置后才能运行预演/);
  assert.match(html, /当前还缺少：环境（先创建并启用）、项目（先创建并启用）、模型与路由（先完成配置）/);
  assert.match(html, /<details class="policy-dry-run-setup"\s*>/);
  assert.doesNotMatch(html, /<details class="policy-dry-run-setup" open>/);
  assert.match(html, /type="submit" disabled/);
});

test("lifecycle aborts stale operations and identity gate includes role and session", () => {
  const lifecycle = createPolicyDryRunLifecycle();
  const first = lifecycle.begin({ identity: "tenant-a-admin", navigation: "policy-simulator" });
  const second = lifecycle.begin({ identity: "tenant-a-admin", navigation: "policy-simulator" });
  assert.equal(first.signal.aborted, true);
  assert.equal(lifecycle.isCurrent(first, { identity: "tenant-a-admin", navigation: "policy-simulator" }), false);
  assert.equal(lifecycle.isCurrent(second, { identity: "tenant-a-admin", navigation: "policy-simulator" }), true);
  assert.equal(lifecycle.isCurrent(second, { identity: "tenant-b-admin", navigation: "policy-simulator" }), false);
  lifecycle.invalidate();
  assert.equal(second.signal.aborted, true);
  assert.equal(lifecycle.isBusy(), false);

  assert.equal(
    policyDryRunIdentitySnapshot(authContext),
    JSON.stringify([
      "tenant_1",
      "user_1",
      "admin",
      "session_1",
      "",
      1,
      "test_authz_admin_1",
      PERMISSIONS.POLICY_SIMULATE,
    ]),
  );
  assert.equal(shouldOwnPolicyDryRun({ canSimulate: true, activeNavigationItemId: "policy-simulator", identity: "x" }), true);
  assert.equal(shouldOwnPolicyDryRun({ canSimulate: false, activeNavigationItemId: "policy-simulator", identity: "x" }), false);
  assert.equal(shouldOwnPolicyDryRun({ canSimulate: true, activeNavigationItemId: "policies", identity: "x" }), false);
});

test("developer-start dispatches simulator only for authorized admin navigation", () => {
  const shellState = {
    activeNavigationItemId: "policy-simulator",
    navigation: {
      active_navigation_item_id: "policy-simulator",
      navigation_items: [{
        id: "policy-simulator",
        label: "策略模拟器",
        group: "安全与治理",
        target_section: "policy-simulator",
        description: "策略模拟器",
        order: 88,
      }],
    },
  };
  const adminContainer = { innerHTML: "" };
  const developerContainer = { innerHTML: "" };
  renderDeveloperStart(adminContainer, {
    authContext,
    consoleShellState: shellState,
    policyDryRunState: createEmptyPolicyDryRunState(),
    ...contextState,
  });
  renderDeveloperStart(developerContainer, {
    authContext: withPermissionSnapshot({
      ...authContext,
      user: { ...authContext.user, role: "developer" },
    }, []),
    consoleShellState: shellState,
    policyDryRunState: createEmptyPolicyDryRunState(),
    ...contextState,
  });
  assert.match(adminContainer.innerHTML, /class="panel policy-dry-run-panel"/);
  assert.match(adminContainer.innerHTML, /id="policy-dry-run-form"/);
  assert.doesNotMatch(developerContainer.innerHTML, /policy-dry-run-panel/);
});

test("main and CSS include every prompt cleanup and responsive boundary", async () => {
  const [mainSource, featureSource, clientSource, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/policy-dry-run/renderPolicyDryRunPanel.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/policyDryRun.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /clearPolicyDryRunSessionState\(\)/);
  assert.match(mainSource, /clearPolicyDryRunStateOnIdentityChange/);
  assert.doesNotMatch(mainSource, /window\.setInterval\(\(\) => \{[\s\S]*?void refresh\(\);/);
  assert.match(mainSource, /case "policy-simulator":[\s\S]*policyDryRunContextState/);
  assert.match(mainSource, /textarea\[name='prompt'\]"\)\.value = ""/);
  assert.match(mainSource, /bindPolicyDryRunInteractions\(consoleShellState\)/);
  assert.match(mainSource, /focusSelector: "#policy-dry-run-summary-title"/);
  assert.match(mainSource, /focusSelector: "\.policy-dry-run-error"/);
  assert.match(mainSource, /focusSelector: "textarea\[name='prompt'\]"/);
  assert.doesNotMatch(featureSource, /(?:localStorage|sessionStorage|indexedDB)\s*\./i);
  assert.doesNotMatch(clientSource, /tenant(?:Id|_id)\s*:/);
  assert.doesNotMatch(mainSource, /policyDryRunState\s*=\s*\{[^}]*prompt\s*:/s);
  assert.match(css, /#policy-dry-run-form fieldset[\s\S]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(max-width: 1300px\)[\s\S]*\.policy-dry-run-governance-grid/);
  assert.match(css, /@media \(max-width: 1100px\)[\s\S]*#policy-dry-run-form fieldset[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.policy-dry-run-table-head[\s\S]*clip-path: inset\(50%\)/);
  assert.doesNotMatch(css, /\.policy-dry-run-table-head\s*\{[^}]*display:\s*none/s);
  assert.match(css, /@media \(max-width: 390px\)[\s\S]*\.policy-dry-run-rule-table/);
});
