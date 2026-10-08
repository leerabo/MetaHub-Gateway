/**
 * 模块职责：验证 PRD-068 PAGE-095 API 客户端的精确路由、双 CAS、绑定白名单与错误脱敏。
 * 安全边界：测试只使用公开假 ID，不输出 Token、tenant、策略表达式、价格或预算事实。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  applyPolicyTemplate,
  listPolicyTemplates,
} from "../../src/lib/policyTemplates.js";
import {
  createPolicyTemplatePageState,
  policyTemplateApplyIdentitySnapshot,
  policyTemplateReadIdentitySnapshot,
} from "../../src/features/policy-templates/policyTemplatePageState.js";
import { renderPolicyTemplates } from "../../src/features/policy-templates/renderPolicyTemplates.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";

// 固定假模板与哈希只用于验证协议边界，不对应任何真实租户、资源或预览事实。
const TEMPLATE_ID = "ptpl_low_cost_mode";
const HASH = "a".repeat(64);
const APPLICATION_ID = `ptapp_${"1".repeat(32)}`;
const GENERATED_POLICY_ID = `pol_${"2".repeat(32)}`;
const SHADOWING_POLICY_ID = `pol_${"3".repeat(32)}`;
const PREVIOUS_SNAPSHOT_ID = `psnap_${"4".repeat(32)}`;
const RESULT_SNAPSHOT_ID = `psnap_${"5".repeat(32)}`;

/** 构造带完整后端授权镜像的认证上下文，前端不得依据 role 字段补权。 */
function authContext(permissionCodes = [
  PERMISSIONS.CONSOLE_BASIC_READ,
  PERMISSIONS.POLICY_TEMPLATE_READ,
  PERMISSIONS.POLICY_TEMPLATE_APPLY,
]) {
  return {
    permission_codes: permissionCodes,
    permission_revision: 5,
    authorization_fingerprint: HASH,
    tenant: { tenant_id: "tenant_a" },
    session: { session_id: "session_a", session_revision: 3 },
    user: {
      tenant_id: "tenant_a",
      user_id: "user_a",
      role: "admin",
      permission_codes: permissionCodes,
      permission_revision: 5,
      authorization_fingerprint: HASH,
    },
  };
}

/** 构造五条固定目录；目标模板可按 overrides 注入精确预览字段。 */
function catalog(overrides = {}) {
  const definitions = [
    ["ptpl_low_cost_mode", "low_cost_mode", "低成本模式", "cost", "route_to", 1000],
    ["ptpl_high_security_mode", "high_security_mode", "高安全模式", "security", "deny", 10],
    ["ptpl_domestic_models_only", "domestic_models_only", "只用国产模型", "security", "deny", 10],
    ["ptpl_budget_burst_guard", "budget_burst_guard", "预算防爆", "budget", "deny", 10],
    ["ptpl_customer_service_redaction", "customer_service_redaction", "客服脱敏", "privacy", "mask", 10],
  ];
  return definitions.map(([id, code, name, category, actionType, priority], index) => ({
    policy_template_id: id,
    tenant_id: "tenant_a",
    template_code: code,
    name,
    description: `${name}说明`,
    category,
    scenario: code,
    status: "active",
    version: 1,
    priority,
    condition_blueprint: "服务端固定条件",
    action_type: actionType,
    required_bindings: code === "high_security_mode"
      ? ["project_id", "model_alias_id"]
      : (code === "budget_burst_guard" ? ["project_id", "threshold_cny"] : ["project_id"]),
    binding_options: {
      projects: [{ project_id: "project_a", name: "客户中心", status: "active" }],
      model_aliases: [{
        model_alias_id: "alias_a",
        alias: "approved",
        environment_id: "env_a",
        environment_code: "prod",
        status: "active",
      }],
    },
    compatibility: {
      status: "unbound",
      reason_codes: [],
      warning_codes: [],
      can_apply: false,
      requires_permissions: ["policy_template.apply"],
    },
    ...(index === 0 ? overrides : {}),
  }));
}

/** 构造 low_cost_mode 的完整精确影响响应，用于验证预览事实与双 CAS 的绑定关系。 */
function previewPayload() {
  return {
    policy_templates: catalog({
      compatibility: {
        status: "ready",
        reason_codes: [],
        warning_codes: [],
        can_apply: true,
        requires_permissions: ["policy_template.apply", "routing.write"],
      },
      impact: {
        project_id: "project_a",
        generated_resource_types: ["policy", "policy_snapshot"],
        evaluation_order: [{
          policy_id: GENERATED_POLICY_ID,
          priority: 1000,
          action_type: "route_to",
          generated: true,
          shadowing: "none",
        }],
        before_policy_count: 0,
        after_policy_count: 1,
        derived_target_model_id: "model_a",
        derived_target_model_name: "glm-5.3-flash",
        allowed_model_alias_names: [],
        frozen_environment_codes: [],
        expected_head_version: 7,
        rollback_target_snapshot_id: PREVIOUS_SNAPSHOT_ID,
        shadowing_policy_ids: [],
      },
      preview_nonce: HASH,
      preview_as_of: "2026-08-03T02:00:00Z",
      impact_hash: HASH,
      before_candidate_hash: "b".repeat(64),
      after_candidate_hash: HASH,
      expected_head_version: 7,
    }),
    total: 5,
    request_id: "req_preview",
    trace_id: "trace_preview",
    headEtag: '"7"',
    // Candidate ETag 必须冻结应用前 hash；应用后 hash 不同，防止夹具掩盖真实 CAS 方向错误。
    candidateEtag: `"${"b".repeat(64)}"`,
  };
}

/** 构造不签发 nonce/CAS 的合法阻断预览，验证页面能展示原因而不能进入写路径。 */
function incompatiblePayload() {
  return {
    policy_templates: catalog({
      compatibility: {
        status: "incompatible",
        reason_codes: ["policy_mask_runtime_required"],
        warning_codes: [],
        can_apply: false,
        requires_permissions: ["policy_template.apply", "policy.write"],
      },
      impact: null,
      preview_nonce: null,
      preview_as_of: null,
      impact_hash: null,
      before_candidate_hash: null,
      after_candidate_hash: null,
      expected_head_version: null,
    }),
    total: 5,
    request_id: "req_incompatible",
    trace_id: "trace_incompatible",
    headEtag: null,
    candidateEtag: null,
  };
}

/** 构造状态机 owner；读写身份必须分别来自精确权限。 */
function pageOwner(context = authContext(), refreshGeneration = 1) {
  return {
    readIdentity: policyTemplateReadIdentitySnapshot(context),
    applyIdentity: policyTemplateApplyIdentitySnapshot(context),
    navigation: "policy-templates",
    refreshGeneration,
  };
}

/** 构造带可控 Header 的成功响应，供目录和 apply 两条路径复用。 */
function response(payload, headers = {}) {
  return {
    ok: true,
    status: 200,
    headers: { get(name) { return headers[name] ?? null; } },
    async json() { return payload; },
  };
}

test("policy template client uses exact preview query and no tenant fields", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return response(
      { policy_templates: [], total: 5, request_id: options.headers["X-Request-ID"] },
      { ETag: '"7"', "X-Policy-Candidate-ETag": `"${HASH}"` },
    );
  };
  const result = await listPolicyTemplates({
    controlApiUrl: "http://control.local",
    token: "jwt-canary",
    templateId: TEMPLATE_ID,
    bindings: { project_id: "project_a" },
    requestId: "console_policy_template_list_1",
  });
  const parsed = new URL(calls[0].url);
  assert.equal(parsed.pathname, "/policy-templates");
  assert.deepEqual([...parsed.searchParams.keys()], ["template_id", "project_id"]);
  assert.equal(parsed.searchParams.get("template_id"), TEMPLATE_ID);
  assert.equal(parsed.searchParams.get("project_id"), "project_a");
  assert.equal(parsed.searchParams.has("tenant_id"), false);
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-canary");
  assert.equal(result.headEtag, '"7"');
  assert.equal(result.candidateEtag, `"${HASH}"`);
});

test("policy template requests use the Workspace cookie and CSRF transport", async () => {
  const previousDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=csrf-test" };
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return response({ policy_templates: [], total: 5, request_id: options.headers["X-Request-ID"] });
  };
  try {
    await listPolicyTemplates({
      controlApiUrl: "http://control.local", authentication: "workspace",
      requestId: "console_policy_template_list_workspace",
    });
    await applyPolicyTemplate({
      controlApiUrl: "http://control.local", authentication: "workspace",
      policyTemplateId: TEMPLATE_ID, templateVersion: 1,
      previewNonce: HASH, previewAsOf: "2026-08-03T02:00:00Z", impactHash: HASH,
      bindings: { project_id: "project_a" }, reason: "应用模板",
      headEtag: '"7"', candidateEtag: `"${HASH}"`,
      requestId: "console_policy_template_apply_workspace",
    });
    assert.equal(calls.length, 2);
    assert.equal(calls[0].options.credentials, "include");
    assert.equal(calls[1].options.credentials, "include");
    assert.equal(calls[0].options.headers.Authorization, undefined);
    assert.equal(calls[1].options.headers["X-CSRF-Token"], "csrf-test");
  } finally {
    globalThis.document = previousDocument;
  }
});

test("policy template apply sends only frozen payload and both strong CAS headers", async () => {
  let call;
  globalThis.fetch = async (url, options = {}) => {
    call = { url: String(url), options };
    return response({ application: {}, request_id: options.headers["X-Request-ID"] }, { ETag: '"8"' });
  };
  await applyPolicyTemplate({
    controlApiUrl: "http://control.local",
    token: "jwt-canary",
    policyTemplateId: TEMPLATE_ID,
    templateVersion: 1,
    previewNonce: HASH,
    previewAsOf: "2026-08-03T02:00:00Z",
    impactHash: HASH,
    bindings: { project_id: "project_a" },
    reason: "启用低成本场景",
    headEtag: '"7"',
    candidateEtag: `"${HASH}"`,
    requestId: "console_policy_template_apply_1",
  });
  assert.equal(call.url, `http://control.local/policy-templates/${TEMPLATE_ID}/apply`);
  assert.equal(call.options.method, "POST");
  assert.equal(call.options.headers["If-Match"], '"7"');
  assert.equal(call.options.headers["If-Policy-Candidate-Match"], `"${HASH}"`);
  assert.equal(call.options.headers["X-Request-ID"], "console_policy_template_apply_1");
  const body = JSON.parse(call.options.body);
  assert.deepEqual(Object.keys(body), [
    "template_version",
    "preview_nonce",
    "preview_as_of",
    "impact_hash",
    "bindings",
    "reason",
  ]);
  assert.equal("tenant_id" in body, false);
  assert.equal("condition_expr" in body, false);
  assert.equal("action" in body, false);
});

test("policy template client rejects unknown bindings and weak CAS before fetch", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; };
  await assert.rejects(
    listPolicyTemplates({
      controlApiUrl: "http://control.local",
      token: "jwt",
      templateId: TEMPLATE_ID,
      bindings: { project_id: "project_a", tenant_id: "tenant_other" },
    }),
    (error) => error.code === "policy_template.binding_invalid" && error.status === 422,
  );
  await assert.rejects(
    applyPolicyTemplate({
      controlApiUrl: "http://control.local",
      token: "jwt",
      policyTemplateId: TEMPLATE_ID,
      templateVersion: 1,
      previewNonce: HASH,
      previewAsOf: "2026-08-03T02:00:00Z",
      impactHash: HASH,
      bindings: { project_id: "project_a" },
      reason: "启用模板",
      headEtag: "W/\"7\"",
      candidateEtag: `"${HASH}"`,
      requestId: "console_policy_template_apply_2",
    }),
    (error) => error.code === "policy_template.payload_invalid" && error.status === 422,
  );
  assert.equal(calls, 0);
});

test("policy template API errors keep only public fields", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    headers: { get() { return null; } },
    async json() {
      return {
        code: "policy_template.impact_conflict",
        message: "预览已变化",
        request_id: "req_public",
        trace_id: "trace_public",
        suggestion: "重新预览",
        details: { provider_key: "secret-canary", condition_expr: "private-canary" },
      };
    },
  });
  await assert.rejects(
    listPolicyTemplates({ controlApiUrl: "http://control.local", token: "jwt" }),
    (error) => {
      assert.equal(error.code, "policy_template.impact_conflict");
      assert.equal(error.requestId, "req_public");
      assert.equal(error.traceId, "trace_public");
      assert.equal(error.details, undefined);
      assert.doesNotMatch(JSON.stringify(error), /secret-canary|private-canary/);
      return true;
    },
  );
});

test("policy template owners fail closed without exact permissions", () => {
  const full = authContext();
  assert.match(policyTemplateReadIdentitySnapshot(full), /policy_template\.read/);
  assert.match(policyTemplateApplyIdentitySnapshot(full), /policy_template\.apply/);
  const readOnly = authContext([
    PERMISSIONS.CONSOLE_BASIC_READ,
    PERMISSIONS.POLICY_TEMPLATE_READ,
  ]);
  assert.match(policyTemplateReadIdentitySnapshot(readOnly), /policy_template\.read/);
  assert.equal(policyTemplateApplyIdentitySnapshot(readOnly), "");
  assert.equal(policyTemplateReadIdentitySnapshot({ user: { role: "admin" } }), "");
});

test("policy template state freezes preview intent and destroys it after apply", () => {
  let sequence = 0;
  const state = createPolicyTemplatePageState({ requestIdFactory: (kind) => `req_${kind}_${++sequence}` });
  assert.equal(state.syncOwner(pageOwner()), true);
  const list = state.beginCatalog();
  assert.equal(state.acceptCatalog(list, { policy_templates: catalog(), total: 5 }), true);
  const preview = state.beginPreview();
  assert.equal(preview.policyTemplateId, TEMPLATE_ID);
  assert.deepEqual(preview.bindings, { project_id: "project_a" });
  assert.equal(state.acceptPreview(preview, previewPayload()), true);
  assert.equal(state.openConfirmation(), true);
  assert.equal(state.setReason("启用低成本场景"), true);
  const apply = state.beginApply();
  assert.equal(apply.previewNonce, HASH);
  assert.equal(apply.headEtag, '"7"');
  assert.equal(apply.candidateEtag, `"${"b".repeat(64)}"`);
  assert.equal(state.acceptApply(apply, {
    application: {
      application_id: APPLICATION_ID,
      policy_template_id: TEMPLATE_ID,
      impact_hash: HASH,
      generated_policy_id: GENERATED_POLICY_ID,
    },
    policy_snapshot: { policy_snapshot_id: RESULT_SNAPSHOT_ID, snapshot_version: 8 },
    idempotent_replay: false,
    request_id: "req_apply_public",
    trace_id: "trace_apply_public",
  }), true);
  const snapshot = state.snapshot();
  assert.equal(snapshot.preview, null);
  assert.equal(snapshot.confirmation, null);
  assert.equal(snapshot.result.snapshotVersion, 8);
  assert.equal(snapshot.result.applicationId, APPLICATION_ID);
  assert.equal(snapshot.result.snapshotId, RESULT_SNAPSHOT_ID);
  assert.doesNotMatch(JSON.stringify(snapshot), new RegExp(HASH));
});

test("policy template confirmation and success render complete public facts", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  assert.equal(state.acceptPreview(preview, previewPayload()), true);
  assert.equal(state.openConfirmation(), true);
  let html = renderPolicyTemplates(state.snapshot());
  assert.match(html, /应用项目/);
  assert.match(html, /客户中心/);
  assert.match(html, /执行动作[\s\S]*定向路由/);
  assert.match(html, /新增规则[\s\S]*1 条/);
  assert.match(html, /上线版本[\s\S]*v8/);
  assert.match(html, /将使用的模型[\s\S]*glm-5\.3-flash/);
  assert.match(html, /原有模型路由的候选顺序和故障切换不会用于这些请求/);
  assert.doesNotMatch(html, new RegExp(GENERATED_POLICY_ID));
  assert.doesNotMatch(html, new RegExp(PREVIOUS_SNAPSHOT_ID));

  state.setReason("确认公开影响摘要");
  const apply = state.beginApply();
  assert.equal(state.acceptApply(apply, {
    application: {
      application_id: APPLICATION_ID,
      policy_template_id: TEMPLATE_ID,
      impact_hash: HASH,
      generated_policy_id: GENERATED_POLICY_ID,
    },
    policy_snapshot: { policy_snapshot_id: RESULT_SNAPSHOT_ID, snapshot_version: 8 },
    idempotent_replay: false,
    request_id: "req_apply_visible",
    trace_id: "trace_apply_visible",
  }), true);
  html = renderPolicyTemplates(state.snapshot());
  assert.match(html, /已为 客户中心 应用“低成本模式”/);
  assert.match(html, /上线版本为 v8/);
  assert.match(html, /查看上线版本/);
  assert.doesNotMatch(html, new RegExp(APPLICATION_ID));
  assert.doesNotMatch(html, new RegExp(RESULT_SNAPSHOT_ID));
});

test("policy template confirmation stays bounded and wraps identifiers on mobile", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  // 固定遮罩不能参与页面文档宽度；对话框与长 ID 必须在 390px 视口内自行收敛。
  assert.match(css, /\.policy-template-page > \.modal-backdrop\s*\{[\s\S]*?position:\s*fixed;[\s\S]*?inset:\s*0;[\s\S]*?overflow:\s*hidden;/);
  assert.match(css, /\.policy-template-confirm\s*\{[\s\S]*?box-sizing:\s*border-box;[\s\S]*?max-height:\s*calc\(100dvh - 40px\);[\s\S]*?overflow:\s*auto;/);
  assert.match(css, /\.policy-template-confirm code\s*\{[\s\S]*?max-width:\s*100%;[\s\S]*?white-space:\s*normal;[\s\S]*?overflow-wrap:\s*anywhere;/);
});

test("policy template confirmation keeps server shadowing warning visible", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  const payload = previewPayload();
  const template = payload.policy_templates[0];
  template.compatibility.status = "ready_with_warnings";
  template.compatibility.warning_codes = ["policy_shadowing_warning"];
  template.impact.evaluation_order.unshift({
    policy_id: SHADOWING_POLICY_ID,
    priority: 10,
    action_type: "deny",
    generated: false,
    shadowing: "warning",
  });
  template.impact.before_policy_count = 1;
  template.impact.after_policy_count = 2;
  template.impact.shadowing_policy_ids = [SHADOWING_POLICY_ID];
  assert.equal(state.acceptPreview(preview, payload), true);
  assert.equal(state.openConfirmation(), true);
  const html = renderPolicyTemplates(state.snapshot());
  assert.match(html, /应用提醒/);
  assert.match(html, /现有策略会改变模板效果/);
  assert.doesNotMatch(html, /1 条现有策略/);
});

test("failed application stays visible beside the action and offers a new check", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  state.acceptPreview(preview, previewPayload());
  state.openConfirmation();
  state.setReason("验证失败提示");
  const apply = state.beginApply();
  state.reject("apply", apply, {
    status: 409,
    code: "policy_template.impact_conflict",
    message: "Conflict",
    requestId: "req_public_1",
  });
  const html = renderPolicyTemplates(state.snapshot());
  assert.match(html, /role="alertdialog"/);
  assert.match(html, /模板未应用/);
  assert.match(html, /配置在检查后发生变化/);
  assert.match(html, /重新检查/);
  assert.doesNotMatch(html, /data-policy-template-modal/);
  assert.equal(state.dismissFeedback(), true);
  assert.doesNotMatch(renderPolicyTemplates(state.snapshot()), /role="alertdialog"/);
});

test("policy template state renders incompatible preview without write credentials", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  assert.equal(state.acceptPreview(preview, incompatiblePayload()), true);
  const snapshot = state.snapshot();
  assert.equal(snapshot.preview.impact, null);
  assert.equal(snapshot.preview.template.compatibility.canApply, false);
  assert.equal(state.openConfirmation(), false);
  const html = renderPolicyTemplates(snapshot);
  assert.match(html, /Gateway 脱敏运行能力尚未全部就绪/);
  assert.match(html, /data-policy-template-remediation="system-status"/);
  assert.match(html, /前往系统状态/);
  assert.doesNotMatch(html, /data-policy-template-action="confirm"/);
  assert.doesNotMatch(html, /preview_nonce|impact_hash|candidateEtag/);
});

test("policy template page treats catalog bindings as an unchecked form, not a user error", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  const items = catalog({
    compatibility: {
      status: "unbound",
      reason_codes: ["bindings_required"],
      warning_codes: [],
      can_apply: false,
      requires_permissions: ["policy_template.apply"],
    },
  });
  assert.equal(state.acceptCatalog(list, { policy_templates: items, total: 5 }), true);
  const html = renderPolicyTemplates(state.snapshot());
  assert.match(html, /要应用到哪个项目？/);
  assert.match(html, /value="project_a"\s+selected/);
  assert.match(html, /2\. 检查能否使用/);
  assert.match(html, /检查不会修改配置/);
  assert.doesNotMatch(html, /请选择模板要求的绑定|待绑定/);
  assert.doesNotMatch(html, /policy-template-compatibility__reason/);
  assert.doesNotMatch(html, /novalidate/);
});

test("policy template check lists both missing model and first publication with separate routes", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  const payload = incompatiblePayload();
  payload.policy_templates[0].compatibility.reason_codes = [
    "eligible_low_cost_model_required",
    "snapshot_baseline_required",
  ];
  assert.equal(state.acceptPreview(preview, payload), true);
  const html = renderPolicyTemplates(state.snapshot());
  assert.match(html, /没有可用于低成本模式的模型/);
  assert.match(html, /无需向量能力/);
  assert.match(html, /成本优先/);
  assert.match(html, /data-policy-template-remediation="models"/);
  assert.match(html, /data-policy-template-remediation="model-aliases"/);
  assert.match(html, /还没有可回退的上线版本/);
  assert.match(html, /可以发布空版本/);
  assert.match(html, /data-policy-template-remediation="policy-snapshots"/);
  assert.match(html, /policy-template-catalog__item is-active[\s\S]*?暂不能应用/);
  assert.doesNotMatch(html, /data-policy-template-action="confirm"|请选择模板要求的绑定/);
});

test("budget scenario validates its required amount before checking", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  assert.equal(state.selectTemplate("ptpl_budget_burst_guard"), true);
  const html = renderPolicyTemplates(state.snapshot());
  assert.match(html, /预算低于多少元时阻止请求？/);
  assert.match(html, /pattern="\(\?:0\|\[1-9\]/);
  assert.match(html, /\\\.\[0-9\]\{1,6\}/);
  assert.match(html, /name="threshold_cny"[\s\S]*?required/);
});

test("policy template state rejects incompatible response carrying CAS credentials", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  const payload = incompatiblePayload();
  payload.headEtag = '"7"';
  assert.equal(state.acceptPreview(preview, payload), false);
  const snapshot = state.snapshot();
  assert.equal(snapshot.preview, null);
  assert.equal(snapshot.pageError.code, "policy_template.unavailable");
});

test("policy template state renders incompatible impact order without apply", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  const payload = previewPayload();
  const template = payload.policy_templates[0];
  template.compatibility = {
    status: "incompatible",
    reason_codes: ["policy_shadowing_conflict"],
    warning_codes: [],
    can_apply: false,
    requires_permissions: ["policy_template.apply", "routing.write"],
  };
  template.impact.evaluation_order.unshift({
    policy_id: SHADOWING_POLICY_ID,
    priority: 10,
    action_type: "allow",
    generated: false,
    shadowing: "blocking",
  });
  template.impact.before_policy_count = 1;
  template.impact.after_policy_count = 2;
  template.impact.shadowing_policy_ids = [SHADOWING_POLICY_ID];
  template.preview_nonce = null;
  template.preview_as_of = null;
  template.impact_hash = null;
  template.before_candidate_hash = null;
  template.after_candidate_hash = null;
  template.expected_head_version = null;
  payload.headEtag = null;
  payload.candidateEtag = null;
  assert.equal(state.acceptPreview(preview, payload), true);
  const snapshot = state.snapshot();
  assert.equal(snapshot.preview.impact.after_policy_count, 2);
  assert.equal(state.openConfirmation(), false);
  const html = renderPolicyTemplates(snapshot);
  assert.match(html, /现有策略会阻断或绕过模板效果/);
  assert.match(html, /pol_333333…333333/);
  assert.match(html, /前往自定义规则/);
  assert.doesNotMatch(html, /data-policy-template-action="confirm"/);
});

test("policy template state rejects preview whose affected Project drifted", () => {
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  const payload = previewPayload();
  payload.policy_templates[0].impact.project_id = "project_other";
  assert.equal(state.acceptPreview(preview, payload), false);
  const snapshot = state.snapshot();
  assert.equal(snapshot.preview, null);
  assert.equal(snapshot.pageError.code, "policy_template.unavailable");
});

test("policy template state fails closed on damaged apply identifiers and version", () => {
  const invalidResults = [
    {
      application_id: "ptapp_invalid",
      generated_policy_id: GENERATED_POLICY_ID,
      snapshot_id: RESULT_SNAPSHOT_ID,
      snapshot_version: 8,
    },
    {
      application_id: APPLICATION_ID,
      generated_policy_id: `pol_${"9".repeat(32)}`,
      snapshot_id: RESULT_SNAPSHOT_ID,
      snapshot_version: 8,
    },
    {
      application_id: APPLICATION_ID,
      generated_policy_id: GENERATED_POLICY_ID,
      snapshot_id: "psnap_invalid",
      snapshot_version: 8,
    },
    {
      application_id: APPLICATION_ID,
      generated_policy_id: GENERATED_POLICY_ID,
      snapshot_id: RESULT_SNAPSHOT_ID,
      snapshot_version: 9,
    },
  ];
  for (const invalid of invalidResults) {
    const state = createPolicyTemplatePageState();
    state.syncOwner(pageOwner());
    const list = state.beginCatalog();
    state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
    const preview = state.beginPreview();
    state.acceptPreview(preview, previewPayload());
    state.openConfirmation();
    state.setReason("拒绝损坏响应");
    const apply = state.beginApply();
    assert.equal(state.acceptApply(apply, {
      application: {
        application_id: invalid.application_id,
        policy_template_id: TEMPLATE_ID,
        impact_hash: HASH,
        generated_policy_id: invalid.generated_policy_id,
      },
      policy_snapshot: {
        policy_snapshot_id: invalid.snapshot_id,
        snapshot_version: invalid.snapshot_version,
      },
    }), false);
    const snapshot = state.snapshot();
    assert.equal(snapshot.result, null);
    assert.equal(snapshot.preview, null);
    assert.equal(snapshot.pageError.code, "policy_template.unavailable");
  }
});

test("policy template state rejects late response and clears data on permission loss", () => {
  const state = createPolicyTemplatePageState();
  assert.equal(state.syncOwner(pageOwner()), true);
  const stale = state.beginCatalog();
  assert.equal(state.syncOwner(pageOwner(authContext([
    PERMISSIONS.CONSOLE_BASIC_READ,
    PERMISSIONS.POLICY_TEMPLATE_READ,
  ]), 2)), true);
  assert.equal(state.acceptCatalog(stale, { policy_templates: catalog(), total: 5 }), false);
  assert.equal(state.snapshot().templates.length, 0);
  assert.equal(state.syncOwner({ navigation: "policy-templates", refreshGeneration: 3 }), false);
  assert.equal(state.snapshot().authorized, false);
});

test("policy template renderer hides unauthorized DOM and never renders nonce or hashes", () => {
  assert.equal(renderPolicyTemplates({ authorized: false }), "");
  const state = createPolicyTemplatePageState();
  state.syncOwner(pageOwner());
  const list = state.beginCatalog();
  state.acceptCatalog(list, { policy_templates: catalog(), total: 5 });
  const preview = state.beginPreview();
  state.acceptPreview(preview, previewPayload());
  const html = renderPolicyTemplates(state.snapshot());
  assert.match(html, /按场景设置规则/);
  assert.match(html, /检查不会修改配置/);
  assert.match(html, /低成本模式/);
  assert.match(html, /应用模板/);
  assert.match(html, /本次模板/);
  assert.doesNotMatch(html, new RegExp(HASH));
  assert.doesNotMatch(html, /preview_nonce|impact_hash|candidateEtag|tenant_a/);
});
