/**
 * 模块职责：验证 PRD-067 PAGE-094 API、owner、五 lane、CAS、敏感 DOM 与 Shell 接入。
 * 安全边界：测试正文只用 canary 断言不进入 HTML/错误；不输出真实 Prompt、Token 或 tenant 数据。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createPromptTemplatePageState,
  promptTemplateReadIdentitySnapshot,
  promptTemplateWriteIdentitySnapshot,
} from "../../src/features/prompt-templates/promptTemplatePageState.js";
import { renderPromptTemplates } from "../../src/features/prompt-templates/renderPromptTemplates.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import {
  createPromptTemplate,
  getPromptTemplate,
  getPromptTemplateOptions,
  getPromptTemplateVersion,
  listPromptTemplates,
  publishPromptTemplateVersion,
  updatePromptTemplate,
} from "../../src/lib/promptTemplates.js";

// 固定公开 ID 和假授权指纹只用于构造可重复测试，不代表真实租户或 Session 身份。
const AUTH_HASH = "a".repeat(64);
const TEMPLATE_ID = "ptpl_template_a";
const VERSION_ID = "ptv_version_a";

/** 构造同时带顶层和 user 镜像的完整授权上下文，不依赖角色名补权。 */
function authContext(permissionCodes = [
  PERMISSIONS.CONSOLE_BASIC_READ,
  PERMISSIONS.PROMPT_TEMPLATE_READ,
  PERMISSIONS.PROMPT_TEMPLATE_WRITE,
]) {
  return {
    permission_codes: permissionCodes,
    permission_revision: 4,
    authorization_fingerprint: AUTH_HASH,
    tenant: { tenant_id: "tenant_a" },
    session: { session_id: "session_a", session_revision: 9 },
    user: {
      tenant_id: "tenant_a",
      user_id: "user_a",
      email: "admin@example.com",
      role: "admin",
      permission_codes: permissionCodes,
      permission_revision: 4,
      authorization_fingerprint: AUTH_HASH,
    },
  };
}

/**
 * 构造 PAGE-094 read/write owner。
 *
 * 参数 context 提供完整认证镜像，refreshGeneration 冻结页面代次；返回值明确不包含 Token。
 */
function pageOwner(context = authContext(), refreshGeneration = 1) {
  return {
    readIdentity: promptTemplateReadIdentitySnapshot(context),
    writeIdentity: promptTemplateWriteIdentitySnapshot(context),
    navigation: "prompt-templates",
    refreshGeneration,
  };
}

/** 构造列表安全投影；不包含正文和变量定义。 */
function templateProjection(overrides = {}) {
  return {
    prompt_template_id: TEMPLATE_ID,
    name: "客服回复",
    description: "客户沟通模板",
    status: "active",
    metadata_revision: 1,
    current_version_id: VERSION_ID,
    current_version_number: 1,
    project_count: 1,
    recommended_model_count: 1,
    created_at: "2026-08-02T08:00:00Z",
    updated_at: "2026-08-02T08:00:00Z",
    ...overrides,
  };
}

/** 构造不可变版本元数据；正文由独立 definition GET 返回。 */
function versionProjection(overrides = {}) {
  return {
    prompt_template_version_id: VERSION_ID,
    prompt_template_id: TEMPLATE_ID,
    version_number: 1,
    state: "published",
    project_scope: "selected",
    project_ids: ["project_a"],
    recommended_model_alias_ids: ["alias_a"],
    variable_count: 1,
    project_count: 1,
    recommended_model_count: 1,
    published_at: "2026-08-02T08:00:00Z",
    ...overrides,
  };
}

/** 构造模板表单最小 options，拒绝借模板权限获取 Project/Model 管理元数据。 */
function templateOptions() {
  return {
    projects: [{ project_id: "project_a", name: "客户中心", status: "active" }],
    model_aliases: [
      {
        model_alias_id: "alias_a",
        alias: "balanced",
        display_name: "企业均衡模型",
        environment_id: "env_a",
        status: "active",
      },
      {
        model_alias_id: "alias_b",
        alias: "quality",
        display_name: "高质量模型",
        environment_id: "env_a",
        status: "active",
      },
      {
        model_alias_id: "alias_c",
        alias: "economy",
        display_name: "经济模型",
        environment_id: "env_a",
        status: "active",
      },
    ],
  };
}

/** 构造已选择列表和详情的状态机，供 definition、mutation 与 renderer 用例复用。 */
function initializedState(requestIdFactory = (() => {
  let sequence = 0;
  return (kind) => `req_${kind}_${++sequence}`;
})()) {
  const state = createPromptTemplatePageState({ requestIdFactory });
  assert.equal(state.syncOwner(pageOwner()), true);
  const list = state.beginList();
  assert.equal(state.acceptList(list, {
    prompt_templates: [templateProjection()], total: 1,
  }, templateOptions()), true);
  const detail = state.beginDetail();
  assert.equal(state.acceptDetail(detail, {
    template: templateProjection(), versions: [versionProjection()],
  }), true);
  return state;
}

test("prompt template permissions build separate read and write owners", () => {
  const full = authContext();
  assert.match(promptTemplateReadIdentitySnapshot(full), /prompt_template\.read/);
  assert.match(promptTemplateWriteIdentitySnapshot(full), /prompt_template\.write/);
  const readOnly = authContext([
    PERMISSIONS.CONSOLE_BASIC_READ,
    PERMISSIONS.PROMPT_TEMPLATE_READ,
  ]);
  assert.match(promptTemplateReadIdentitySnapshot(readOnly), /prompt_template\.read/);
  assert.equal(promptTemplateWriteIdentitySnapshot(readOnly), "");
  assert.equal(promptTemplateReadIdentitySnapshot({ user: { role: "admin" } }), "");
});

test("prompt template API uses exact routes, write request ids and no tenant fields", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: options.method === "POST" ? 201 : 200,
      headers: { get() { return null; } },
      async json() { return { request_id: options.headers?.["X-Request-ID"], trace_id: "trace_a" }; },
    };
  };
  await listPromptTemplates({
    controlApiUrl: "http://control.local", token: "jwt", status: "active", search: "客服", limit: 20, offset: 0,
  });
  await getPromptTemplateOptions({ controlApiUrl: "http://control.local", token: "jwt" });
  await getPromptTemplate({ controlApiUrl: "http://control.local", token: "jwt", promptTemplateId: TEMPLATE_ID });
  await getPromptTemplateVersion({
    controlApiUrl: "http://control.local",
    token: "jwt",
    promptTemplateId: TEMPLATE_ID,
    promptTemplateVersionId: VERSION_ID,
  });
  const definition = {
    content: "${customer_name}",
    variables: [{ name: "customer_name", label: "客户名称", required: true, max_chars: 80 }],
    project_scope: "selected",
    project_ids: ["project_a"],
    recommended_model_alias_ids: ["alias_a"],
    change_summary: "初始版本",
  };
  await createPromptTemplate({
    controlApiUrl: "http://control.local",
    token: "jwt",
    payload: { name: "客服回复", description: "描述", ...definition },
    requestId: "req_create_stable",
  });
  await updatePromptTemplate({
    controlApiUrl: "http://control.local",
    token: "jwt",
    promptTemplateId: TEMPLATE_ID,
    payload: { expected_revision: 1, name: "客服回复", description: "新描述", status: "active" },
    requestId: "req_patch_stable",
  });
  await publishPromptTemplateVersion({
    controlApiUrl: "http://control.local",
    token: "jwt",
    promptTemplateId: TEMPLATE_ID,
    payload: { expected_revision: 2, ...definition, change_summary: "第二版" },
    requestId: "req_publish_stable",
  });
  assert.match(calls[0].url, /\/prompt-templates\?limit=20&offset=0&status=active&search=/);
  assert.equal(calls[1].url, "http://control.local/prompt-templates/options");
  assert.equal(calls[2].url, `http://control.local/prompt-templates/${TEMPLATE_ID}`);
  assert.equal(calls[3].url, `http://control.local/prompt-templates/${TEMPLATE_ID}/versions/${VERSION_ID}`);
  assert.equal(calls[4].options.headers["X-Request-ID"], "req_create_stable");
  assert.equal(calls[5].options.headers["X-Request-ID"], "req_patch_stable");
  assert.equal(calls[6].options.headers["X-Request-ID"], "req_publish_stable");
  for (const call of calls.slice(4)) {
    assert.doesNotMatch(call.options.body, /tenant_id|actor_user_id|authorization_fingerprint/);
  }
});

test("PAGE-094 keeps list, detail and definition lanes independent and rejects late owners", () => {
  const state = createPromptTemplatePageState({ requestIdFactory: (kind) => `req_${kind}` });
  assert.equal(state.syncOwner(pageOwner()), true);
  const list = state.beginList();
  assert.equal(state.acceptList(list, {
    prompt_templates: [templateProjection()], total: 1,
  }, templateOptions()), true);
  const detail = state.beginDetail();
  const secondList = state.beginList();
  assert.equal(detail.signal.aborted, false);
  assert.equal(secondList.signal.aborted, false);
  assert.equal(state.acceptDetail(detail, {
    template: templateProjection(), versions: [versionProjection()],
  }), true);
  const definition = state.beginDefinition(VERSION_ID);
  assert.equal(definition.signal.aborted, false);
  assert.equal(state.syncOwner(pageOwner(authContext(), 2)), true);
  assert.equal(definition.signal.aborted, true);
  assert.equal(state.acceptDefinition(definition, {}), false);
  assert.equal(state.snapshot().definition, null);
});

test("PAGE-094 rejects cross-template version history as an owner-integrity failure", () => {
  const state = createPromptTemplatePageState({ requestIdFactory: (kind) => `req_${kind}` });
  state.syncOwner(pageOwner());
  assert.equal(state.acceptList(state.beginList(), {
    prompt_templates: [templateProjection()], total: 1,
  }, templateOptions()), true);
  const detail = state.beginDetail();
  assert.equal(state.acceptDetail(detail, {
    template: templateProjection(),
    versions: [versionProjection({ prompt_template_id: "ptpl_other_template" })],
  }), false);
  assert.equal(state.snapshot().pageError.code, "prompt_template.integrity_failed");
  assert.equal(state.snapshot().detail, null);
});

test("PAGE-094 definition and form canaries never enter renderer HTML", () => {
  const state = initializedState();
  const canary = "prd067-private-prompt-<script>";
  const definition = state.beginDefinition(VERSION_ID);
  assert.equal(state.acceptDefinition(definition, {
    prompt_template_id: TEMPLATE_ID,
    prompt_template_version_id: VERSION_ID,
    version_number: 1,
    definition: {
      content: canary,
      variables: [{ name: "secret_name", label: "内部字段", required: true, max_chars: 100 }],
      project_scope: "all",
      project_ids: [],
      recommended_model_alias_ids: [],
      change_summary: "private-change-summary-canary",
    },
  }), true);
  assert.equal(state.openPublish(), true);
  state.updateDialog({ content: `${canary}-draft`, change_summary: "draft-summary-canary" });
  const html = renderPromptTemplates(state.snapshot(), authContext());
  assert.match(html, /data-prompt-template-definition-content/);
  assert.match(html, /data-prompt-template-variable-editor/);
  assert.doesNotMatch(html, /prd067-private-prompt|private-change-summary|draft-summary-canary|secret_name|内部字段/);
  assert.equal(renderPromptTemplates(state.snapshot(), authContext([PERMISSIONS.CONSOLE_BASIC_READ])), "");
});

test("PAGE-094 switching selected project scope to all clears stale bindings", () => {
  const state = initializedState();
  const definition = state.beginDefinition(VERSION_ID);
  assert.equal(state.acceptDefinition(definition, {
    prompt_template_id: TEMPLATE_ID,
    prompt_template_version_id: VERSION_ID,
    version_number: 1,
    definition: {
      content: "固定内容",
      variables: [],
      project_scope: "selected",
      project_ids: ["project_a"],
      recommended_model_alias_ids: [],
      change_summary: "初始版本",
    },
  }), true);
  assert.equal(state.openPublish(), true);
  assert.deepEqual(state.snapshot().dialog.draft.project_ids, ["project_a"]);
  assert.equal(state.updateDialog({ project_scope: "all" }), true);
  assert.deepEqual(state.snapshot().dialog.draft.project_ids, []);
  assert.equal(state.setBinding("project", "project_a", true), false);
});

test("PAGE-094 reorders recommended models without changing the selected set", () => {
  const state = initializedState();
  const definition = state.beginDefinition(VERSION_ID);
  assert.equal(state.acceptDefinition(definition, {
    prompt_template_id: TEMPLATE_ID,
    prompt_template_version_id: VERSION_ID,
    version_number: 1,
    definition: {
      content: "固定内容",
      variables: [],
      project_scope: "all",
      project_ids: [],
      recommended_model_alias_ids: ["alias_a", "alias_b", "alias_c"],
      change_summary: "初始版本",
    },
  }), true);
  assert.equal(state.openPublish(), true);
  assert.equal(state.moveRecommendedModel("alias_c", "up"), true);
  assert.deepEqual(
    state.snapshot().dialog.draft.recommended_model_alias_ids,
    ["alias_a", "alias_c", "alias_b"],
  );
  assert.equal(state.moveRecommendedModel("alias_c", "up"), true);
  assert.deepEqual(
    state.snapshot().dialog.draft.recommended_model_alias_ids,
    ["alias_c", "alias_a", "alias_b"],
  );
  assert.equal(state.moveRecommendedModel("alias_c", "down"), true);
  assert.equal(state.moveRecommendedModel("alias_b", "down"), false);
  assert.deepEqual(
    [...state.snapshot().dialog.draft.recommended_model_alias_ids].sort(),
    ["alias_a", "alias_b", "alias_c"],
  );
});

test("PAGE-094 409 preserves local draft and adopts a separately fetched metadata revision", () => {
  let sequence = 0;
  const state = initializedState((kind) => `req_${kind}_${++sequence}`);
  assert.equal(state.openMetadata(), true);
  state.updateDialog({ description: "本地未保存描述" });
  const mutation = state.beginMutation();
  assert.equal(mutation.payload.expected_revision, 1);
  assert.equal(state.rejectMutation(mutation, {
    status: 409,
    code: "prompt_template.revision_conflict",
    message: "conflict",
    requestId: mutation.requestId,
  }), true);
  const conflicted = state.snapshot();
  assert.equal(conflicted.dialog.draft.description, "本地未保存描述");
  assert.equal(conflicted.dialog.baseMetadataRevision, 1);
  const conflict = state.beginConflictDetail();
  assert.notEqual(conflict.requestId, mutation.requestId);
  assert.equal(state.acceptConflictDetail(conflict, {
    template: templateProjection({ metadata_revision: 2, description: "服务器描述" }),
    versions: [versionProjection()],
  }), true);
  assert.equal(state.adoptConflict(), true);
  const adopted = state.snapshot();
  assert.equal(adopted.dialog.baseMetadataRevision, 2);
  assert.equal(adopted.dialog.draft.description, "本地未保存描述");
});

test("PAGE-094 network-unknown retry reuses request id only for the same canonical intent", () => {
  let sequence = 0;
  const state = initializedState((kind) => `req_${kind}_${++sequence}`);
  assert.equal(state.openMetadata(), true);
  state.updateDialog({ description: "same intent" });
  const first = state.beginMutation();
  assert.equal(state.rejectMutation(first, {
    status: 503,
    code: "prompt_template.unavailable",
    message: "unavailable",
  }), true);
  const retry = state.beginMutation();
  assert.equal(retry.requestId, first.requestId);
  assert.equal(state.rejectMutation(retry, { status: 422, code: "prompt_template.request_invalid" }), true);
  state.updateDialog({ description: "changed intent" });
  const changed = state.beginMutation();
  assert.notEqual(changed.requestId, first.requestId);
});

test("PAGE-094 is wired into authorized shell and lifecycle clears sensitive DOM before leave", async () => {
  const state = initializedState();
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: authContext(),
    promptTemplateState: state.snapshot(),
    consoleShellState: {
      state: "ok",
      activeNavigationItemId: "prompt-templates",
      navigation: {
        active_navigation_item_id: "prompt-templates",
        navigation_items: [{
          id: "prompt-templates",
          label: "Prompt 模板",
          target_section: "prompt-templates",
          required_permissions: [PERMISSIONS.PROMPT_TEMPLATE_READ],
          visible: true,
          disabled: false,
        }],
      },
    },
  });
  assert.match(container.innerHTML, /prompt-templates-shell/);
  assert.match(container.innerHTML, /data-prompt-templates-root/);
  const [mainSource, rendererSource, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/prompt-templates/renderPromptTemplates.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /function clearPromptTemplateSessionState\(\)[\s\S]*clearPromptTemplateSensitiveDom\(document\)[\s\S]*promptTemplatePageState\.leave\(\)/);
  assert.match(mainSource, /window\.addEventListener\("pagehide"[\s\S]*clearPromptTemplateSessionState\(\)/);
  assert.match(mainSource, /itemId !== "prompt-templates"[\s\S]*clearPromptTemplateSessionState\(\)/);
  assert.match(mainSource, /promptTemplatePageState\.blocksShellDomReplacement\(\)/);
  assert.match(rendererSource, /content\.textContent\s*=/);
  assert.match(rendererSource, /input\.value\s*=/);
  assert.doesNotMatch(rendererSource, /localStorage|sessionStorage|indexedDB|console\.(?:log|warn|error)/);
  assert.match(css, /\.prompt-template-workspace\s*\{[\s\S]*grid-template-columns:\s*minmax\(260px, 0\.36fr\) minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width:\s*720px\)[\s\S]*data-prompt-template-mobile-view="detail"/);
  // 移动端最后一行必须先放 40px 刷新图标，再让中文命令占满余宽，避免“新建模板”被压成竖排。
  assert.match(css, /@media \(max-width:\s*720px\)[\s\S]*\.prompt-template-filters\s*\{[\s\S]*grid-template-columns:\s*40px minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width:\s*720px\)[\s\S]*\.prompt-template-command\s*\{[\s\S]*width:\s*100%/);
});
