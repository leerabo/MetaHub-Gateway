import assert from "node:assert/strict";
import test from "node:test";
import { renderWorkspaceTaskHome, renderWorkspaceConnectionStatus } from "../../src/features/workspace-navigation/renderWorkspaceNavigation.js";
import { renderModelCatalogPanel } from "../../src/features/models/renderModelCatalogPanel.js";
import { renderModelAliasPanel } from "../../src/features/model-aliases/renderModelAliasPanel.js";
import { renderVirtualKeyPanel } from "../../src/features/virtual-keys/renderVirtualKeyPanel.js";
import { renderProviderPanel } from "../../src/features/providers/renderProviderPanel.js";
import { renderProviderCatalog } from "../../src/features/providers/renderProviderCatalog.js";
import { renderSdkExamples } from "../../src/features/python-sdk-examples/renderPythonSdkExamples.js";
import { syncIdentityEntryView } from "../../src/features/identity/main.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import { bindConsoleSidebarViewport, renderConsoleEntryLoading, revealConsoleSidebarItem } from "../../src/features/console-shell/renderConsoleShell.js";
import { createI18n } from "../../src/i18n/messages.js";

const auth = { permission_codes: Object.values(PERMISSIONS) };

function progress(statuses = ["completed", "completed", "completed", "ready"]) {
  return { source_freshness: "fresh", tasks: [
    ["provider_connected", "workspace.gateway.providers"],
    ["model_catalog_ready", "workspace.gateway.models"],
    ["project_key_ready", "workspace.gateway.virtual-keys"],
    ["first_gateway_call", "workspace.developer.sdk"],
  ].map(([task_code, action_route_code], index) => ({ task_code, action_route_code,
    status: statuses[index], action_enabled: true })) };
}

test("connection pages report the first real gap with a permitted recovery route", () => {
  for (const route of ["workspace.gateway.models", "workspace.gateway.aliases", "workspace.gateway.virtual-keys", "workspace.developer.sdk"]) {
    const html = renderWorkspaceConnectionStatus(progress(["ready", "completed", "completed", "completed"]), route);
    assert.match(html, /组织接入尚未完成/u);
    assert.match(html, /data-workspace-task-route="workspace.gateway.providers"/u);
    assert.doesNotMatch(html, /已记录成功调用/u);
  }
  const current = renderWorkspaceConnectionStatus(progress(["completed", "ready", "completed", "ready"]), "workspace.gateway.models");
  assert.match(current, /模型与路由待就绪/u);
  assert.doesNotMatch(current, /<a /u);
});

test("first call is usage evidence and does not block administrator delivery", () => {
  const pending = renderWorkspaceConnectionStatus(progress(), "workspace.developer.sdk");
  assert.match(pending, /接入配置已完成，可开始使用/u);
  assert.match(pending, /首次调用会在员工实际使用后记录/u);
  assert.match(pending, /data-resource-retry>刷新接入状态/u);
  assert.doesNotMatch(pending, /<form|确认完成|等待首次成功调用/u);
  const completed = renderWorkspaceConnectionStatus(progress(Array(4).fill("completed")), "workspace.developer.sdk");
  assert.match(completed, /已记录成功调用/u);
  assert.equal(renderWorkspaceConnectionStatus(progress(), "workspace.gateway.models"), "");
});

test("partial roles, stale facts and forbidden routes cannot imply connection readiness", () => {
  const state = progress(["ready", "completed", "completed", "ready"]);
  for (const action_route_code of [null, "workspace.unknown", '"><img src=x onerror=1>']) {
    state.tasks[0].action_route_code = action_route_code;
    assert.doesNotMatch(renderWorkspaceConnectionStatus(state, "workspace.developer.sdk"), /<a\s|<img\b/u);
  }
  state.tasks[0].action_route_code = "workspace.gateway.providers";
  state.tasks[0].action_enabled = false;
  assert.doesNotMatch(renderWorkspaceConnectionStatus(state, "workspace.developer.sdk"), /<a\s/u);
  assert.equal(renderWorkspaceConnectionStatus({ ...state, tasks: state.tasks.slice(1) }, "workspace.developer.sdk"), "");
  for (const source_freshness of ["stale", "unavailable", undefined]) {
    const html = renderWorkspaceConnectionStatus({ ...progress(), source_freshness }, "workspace.developer.sdk");
    assert.match(html, /接入状态暂不可用/u);
    assert.doesNotMatch(html, /已记录成功调用|等待首次成功调用/u);
  }
});

test("connection status failure preserves usable connection details and SDK disclosure", () => {
  const html = renderSdkExamples({ workspaceTaskHomeState: { status: "error" }, pythonState: {
    state: "ok", gateway_base_url: "https://gateway.example/v1", chat_model_alias: "company-default", chat_model_configured: true,
  } });
  assert.match(html, /接入状态暂不可用/u);
  assert.match(html, /id="tool-service-address">https:\/\/gateway.example/u);
  assert.doesNotMatch(html, /id="tool-gateway-address"/u);
  assert.match(html, /<details class="sdk-developer-details">/u);
});

test("connection details distinguish this-computer trial URLs from employee-accessible deployment", () => {
  for (const address of ["http://127.0.0.1:8000/v1", "http://localhost:8000/v1", "http://[::1]:8000/v1"]) {
    assert.match(renderSdkExamples({ pythonState: { state: "ok", gateway_base_url: address } }), /当前网关地址仅供本机试用/u);
  }
  assert.doesNotMatch(renderSdkExamples({ pythonState: { state: "ok", gateway_base_url: "https://gateway.example/v1" } }), /当前网关地址仅供本机试用/u);
});

test("management pages keep prerequisite recovery without cross-page next-step links", () => {
  const incomplete = progress(["ready", "ready", "completed", "ready"]);
  const models = renderModelCatalogPanel({ state: "ok", models: [{}] }, { state: "ok", providers: [] }, auth, incomplete);
  assert.match(models, /class="workspace-recovery-action" data-workspace-task-route="workspace.gateway.providers"/u);
  assert.doesNotMatch(models, /console-flow-(?:next|related)|配置模型路由/u);
  assert.doesNotMatch(models, /下一步：/u);
  assert.ok(models.indexOf('id="models-title"') < models.indexOf("组织接入尚未完成"));
  const routes = renderModelAliasPanel({ state: "ok", model_aliases: [{}] }, { state: "ok", models: [] }, auth, {}, incomplete);
  assert.doesNotMatch(routes, /console-flow-(?:next|related)|个人 KEY/u);
  assert.doesNotMatch(routes, /下一步：/u);
  const complete = renderModelCatalogPanel({ state: "ok", models: [{}] }, {}, auth, progress());
  assert.doesNotMatch(complete, /下一步：|console-flow-(?:next|related)/u);
});

test("KEY page keeps the personal action short before connection details", () => {
  const keys = { state: "ok", virtual_keys: [{ name: "Organization KEY", status: "active" }] };
  for (const statuses of [["ready", "completed", "completed", "ready"], ["completed", "ready", "completed", "ready"]]) {
    const html = renderVirtualKeyPanel(keys, auth, {}, {}, progress(statuses));
    assert.match(html, /<h2 id="virtual-keys-title">我的 KEY<\/h2>/u);
    assert.doesNotMatch(html, /不需要配置组织 KEY|组织默认环境、模型与路由会自动应用/u);
    assert.doesNotMatch(html, /console-flow-(?:next|related)|接入信息与调用示例/u);
    assert.doesNotMatch(html, /下一步：/u);
  }
  assert.doesNotMatch(renderVirtualKeyPanel(keys, auth, {}, {}, progress()), /下一步：|console-flow-(?:next|related)/u);
  for (const state of [null, { status: "error" }, { ...progress(), source_freshness: "stale" },
    { ...progress(), tasks: progress().tasks.slice(1) }]) {
    assert.doesNotMatch(renderVirtualKeyPanel(keys, auth, {}, {}, state), /下一步：/u);
  }
  const readonly = renderVirtualKeyPanel(keys, { permission_codes: [PERMISSIONS.VIRTUAL_KEY_READ] }, {}, {}, null);
  assert.doesNotMatch(readonly, /href="#workspace.developer.sdk"|创建我的 KEY/u);
});

test("organization KEY list keeps active credentials actionable without employee assignment controls", () => {
  const html = renderVirtualKeyPanel({
    state: "ok",
    virtual_keys: [
      { virtual_key_id: "active-key", consumer_id: "consumer_1", name: "当前 KEY", key_prefix: "mh_live", status: "active", environment: "prod", credential_revision: 2 },
      { virtual_key_id: "old-key", consumer_id: "consumer_1", name: "旧 KEY", key_prefix: "mh_old", status: "disabled", environment: "prod", credential_revision: 1 },
    ],
  }, auth, {}, { state: "ok", environments: [{ environment_id: "env_prod", name: "生产", code: "prod", status: "active" }] }, progress());
  assert.match(html, /data-virtual-key-form data-virtual-key-id="active-key"/u);
  assert.match(html, />禁用<\/button>/u);
  assert.doesNotMatch(html, /consumerId|发放给|员工 KEY/u);
});

test("session transitions replace private content with a public loading and exit state", () => {
  const container = { innerHTML: "old private content" };
  renderConsoleEntryLoading(container);
  assert.match(container.innerHTML, /role="status"[\s\S]*正在加载组织空间/u);
  assert.match(container.innerHTML, /href="\.\/">返回首页/u);
  assert.doesNotMatch(container.innerHTML, /old private content|登录状态已失效/u);
  assert.doesNotThrow(() => renderConsoleEntryLoading(null));
});

test("current sidebar item is revealed without replacing nodes, moving focus or scrolling the page", () => {
  const bounds = { top: 100, bottom: 600 };
  let itemBounds = { top: 650, bottom: 690 };
  let visible = true;
  const item = { getClientRects: () => visible ? [itemBounds] : [], getBoundingClientRect: () => itemBounds };
  const sidebar = { scrollTop: 0, getBoundingClientRect: () => bounds,
    querySelector: (selector) => { assert.equal(selector, '.sidebar-link[aria-current="page"]'); return item; } };
  const container = { querySelector: (selector) => { assert.equal(selector, ".console-sidebar"); return sidebar; } };
  revealConsoleSidebarItem(container);
  assert.equal(sidebar.scrollTop, 98);
  itemBounds = { top: 150, bottom: 190 };
  revealConsoleSidebarItem(container);
  assert.equal(sidebar.scrollTop, 98);
  itemBounds = { top: 80, bottom: 120 };
  revealConsoleSidebarItem(container);
  assert.equal(sidebar.scrollTop, 70);
  visible = false;
  revealConsoleSidebarItem(container);
  assert.equal(sidebar.scrollTop, 70);
  assert.doesNotThrow(() => revealConsoleSidebarItem(null));
});

test("custom provider asks only for a name and URL before optional protocol settings", () => {
  const html = renderProviderCatalog({ state: "ok", entries: [], selectedEntryCode: "custom_openai_compatible" }, auth);
  const form = html.slice(html.indexOf('id="create-provider-form"'), html.indexOf('</form></article>'));
  const basic = form.slice(0, form.indexOf('<details class="provider-custom-advanced"'));
  assert.match(basic, /name="name"[\s\S]*?name="baseUrl"/u);
  assert.doesNotMatch(basic, /name="providerCode"|name="providerType"|name="authType"/u);
  assert.doesNotMatch(form, /name="providerCode"/u);
  assert.match(form, /<button type="submit">添加模型服务/u);
});

test("managed key panels use one dialog and keep errors inside the dialog", () => {
  const html = renderProviderPanel({ state: "ok", providers: [{ provider_id: "p1", auth_type: "bearer", name: "Service" }],
    providerKeysByProviderId: { p1: { state: "error" } } }, auth);
  assert.equal((html.match(/data-provider-key-open=/gu) ?? []).length, 1);
  assert.match(html, /data-provider-key-dialog/u);
  assert.match(html, /密钥列表暂不可用/u);
  assert.doesNotMatch(html, /收起密钥配置|高级设置|<details class="provider-keypool"/u);
});

test("route dependencies remain visible outside a collapsed form and distinguish missing from loading", () => {
  const state = { state: "ok", model_aliases: [{}] };
  const models = { state: "ok", models: [{ status: "active" }] };
  const missing = renderModelAliasPanel(state, models, auth, { state: "ok", environments: [] });
  assert.ok(missing.indexOf("暂无可用运行环境") < missing.indexOf('data-model-alias-dialog'));
  assert.match(missing, /请联系平台管理员完成运行环境配置/u);
  assert.doesNotMatch(missing, /href="#workspace.settings.deployments"/u);
  assert.match(missing, /disabled>创建路由/u);
  assert.match(missing, /<dialog class="model-alias-dialog"/u);
  const loading = renderModelAliasPanel(state, models, auth, null);
  assert.match(loading, /正在加载路由所需配置/u);
  assert.doesNotMatch(loading, /role="status">暂无可用运行环境/u);
});

test("existing KEY records are not buried under an empty creation form", () => {
  const state = { state: "ok", virtual_keys: [{ virtual_key_id: "key_1", status: "active" }] };
  const environment = { state: "ok", environments: [{ environment_id: "env_1", status: "active" }] };
  const html = renderVirtualKeyPanel(state, auth, {}, environment);
  assert.match(html, /<dialog class="virtual-key-dialog"/u);
  assert.doesNotMatch(html, /consumer-create-disclosure|consumerId|发放 KEY/u);
  const firstKey = renderVirtualKeyPanel({ ...state, virtual_keys: [] }, auth, {}, environment);
  assert.match(firstKey, /data-open-virtual-key-dialog/u);
  const afterCreate = renderVirtualKeyPanel({ ...state, keyCreateOpen: true }, auth, {}, environment);
  assert.match(afterCreate, /data-virtual-key-auto-open/u);
});

test("KEY creation keeps the default setup invisible when environment details are unavailable", () => {
  const state = { state: "ok", virtual_keys: [{ virtual_key_id: "key_1" }] };
  const failed = renderVirtualKeyPanel(state, auth, {}, { state: "error" });
  assert.match(failed, /data-open-virtual-key-dialog/u);
  assert.doesNotMatch(failed, /运行环境加载失败|data-resource-retry|disabled>创建/u);
  assert.match(failed, /data-virtual-key-row="key_1"/u);
  const missing = renderVirtualKeyPanel(state, auth, {}, { state: "ok", environments: [] });
  assert.match(missing, /<button type="submit">创建 KEY<\/button>/u);
  assert.doesNotMatch(missing, /暂无可用运行环境|运行环境加载失败/u);
});

test("compact model and KEY records retain field meanings after column headers disappear", () => {
  const models = renderModelCatalogPanel({ state: "ok", models: [{ capabilities: {} }] }, {}, auth);
  for (const label of ["显示名称", "模型名称", "模型服务", "计价", "状态"]) {
    assert.ok(models.includes(`<span class="model-field-label">${label}</span>`));
  }
  assert.doesNotMatch(models, /能力|上下文/u);
  const keys = renderVirtualKeyPanel({ state: "ok", virtual_keys: [{}] }, auth, {}, {});
  for (const label of ["KEY 名称", "密钥前缀", "归属范围", "环境", "状态", "最近使用"]) {
    assert.ok(keys.includes(`<span class="virtual-key-field-label">${label}</span>`));
  }
});

test("owner start flow puts model access before optional administration", () => {
  const html = renderWorkspaceTaskHome({ status: "ready", tasks: [
    ["workspace_profile", "workspace.settings.profile"],
    ["project_key_ready", "workspace.gateway.virtual-keys"],
    ["provider_connected", "workspace.gateway.providers"],
    ["first_gateway_call", "workspace.developer.sdk"],
    ["model_catalog_ready", "workspace.gateway.models"],
  ].map(([task_code, action_route_code]) => ({ task_code, action_route_code, status: "ready", action_enabled: true })) });
  assert.ok(html.indexOf("接入模型服务") < html.indexOf("配置可用模型"));
  assert.ok(html.indexOf("配置可用模型") < html.indexOf("成员 KEY 已启用"));
  assert.match(html, /<details class="workspace-other-tasks">[\s\S]*组织资料/u);
  assert.equal((html.match(/aria-current="step"/gu) ?? []).length, 1);
});

test("employee task home presents one access contract without control-plane vocabulary", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "viewer",
    source_freshness: "fresh",
    tasks: [{ task_code: "service_visibility", status: "completed", action_route_code: null, action_enabled: false }],
  }, undefined, {
    navigation: { gateway_base_url: "https://gateway.example.test", gateway_base_url_status: "ready" },
  });
  assert.match(html, /我的接入/u);
  assert.match(html, /https:\/\/gateway\.example\.test/u);
  assert.match(html, /个人 KEY/u);
  assert.match(html, /成员被撤权后 KEY 会立即失效/u);
  assert.doesNotMatch(html, /预算|路由策略|策略快照/u);
});

test("admin task home groups the setup into four operational work domains", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "admin",
    tasks: [
      ["provider_connected", "workspace.gateway.providers"],
      ["model_catalog_ready", "workspace.gateway.models"],
      ["project_key_ready", "workspace.gateway.virtual-keys"],
      ["first_gateway_call", "workspace.developer.sdk"],
    ].map(([task_code, action_route_code]) => ({ task_code, action_route_code, status: "ready", action_enabled: true })),
  });
  assert.match(html, /管理员工作台/u);
  assert.equal((html.match(/class="workspace-admin-domain"/gu) ?? []).length, 4);
  assert.match(html, /接入与成员/u);
  assert.match(html, /模型与网关/u);
  assert.match(html, /用量与风险/u);
  assert.match(html, /治理与运维/u);
  assert.match(html, /<details class="workspace-admin-workbench-disclosure">/u);
  assert.match(html, /查看管理区域（4）/u);
});

test("english task home keeps the OpenRouter-style role surface in one language", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "admin",
    tasks: [
      { task_code: "provider_connected", status: "ready", action_route_code: "workspace.gateway.providers", action_enabled: true },
      { task_code: "security_policy_ready", status: "ready", action_route_code: "workspace.governance.policies", action_enabled: true },
    ],
  }, createI18n("en-US"));
  assert.match(html, /Admin workspace/u);
  assert.match(html, /Access and members/u);
  assert.match(html, /Models and Gateway/u);
  assert.match(html, /Configure provider/u);
  assert.match(html, /Configure security policy/u);
  assert.doesNotMatch(html, /管理员工作台|接入与成员|任务摘要|四个工作域/u);
  assert.doesNotMatch(html, /配置安全策略|确认策略在当前空间生效/u);
});

test("english employee home presents the same single Gateway and KEY contract", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "viewer",
    source_freshness: "fresh",
    tasks: [],
  }, createI18n("en-US"), {
    navigation: { gateway_base_url: "https://gateway.example.test", gateway_base_url_status: "ready" },
  });
  assert.match(html, /My access/u);
  assert.match(html, /Shared Gateway address/u);
  assert.match(html, /Personal key/u);
  assert.match(html, /Copy shared Gateway address/u);
  assert.doesNotMatch(html, /我的接入|统一 Gateway 地址|个人 MetaHub KEY|员工/u);
});

test("admin setup flow exposes an honest accessible completion progress", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "admin",
    tasks: [
      { task_code: "provider_connected", status: "completed", action_route_code: "workspace.gateway.providers", action_enabled: true },
      { task_code: "model_catalog_ready", status: "ready", action_route_code: "workspace.gateway.models", action_enabled: true },
      { task_code: "project_key_ready", status: "ready", action_route_code: "workspace.gateway.virtual-keys", action_enabled: true },
      { task_code: "first_gateway_call", status: "ready", action_route_code: "workspace.developer.sdk", action_enabled: true },
    ],
  });
  assert.match(html, /role="progressbar"[^>]*aria-valuenow="50"[^>]*aria-valuetext="1 \/ 2"/u);
  assert.match(html, /style="width:50%"/u);
});

test("blocked work domains do not present themselves as ready", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "admin",
    tasks: [{ task_code: "provider_connected", status: "blocked", action_route_code: null, action_enabled: false }],
  });
  assert.match(html, /data-domain-status="blocked"/u);
  assert.match(html, /等待前置条件/u);
  assert.doesNotMatch(html, /data-domain-status="blocked"[\s\S]*已就绪/u);
});

test("finance administrator task home leads with exception-oriented status work", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "finance_admin",
    source_freshness: "fresh",
    tasks: [
      { task_code: "subscription_ready", status: "completed", action_route_code: "workspace.finance.subscription", action_enabled: true },
      { task_code: "cost_controls", status: "needs_attention", action_route_code: "workspace.finance.budgets", action_enabled: true },
      { task_code: "finops_ready", status: "ready", action_route_code: "workspace.finance.internal-finops", action_enabled: true },
    ],
  });
  assert.match(html, /财务工作台/u);
  assert.match(html, /财务与风险/u);
  assert.match(html, /需要关注/u);
  assert.match(html, /查看财务任务明细/u);
  assert.ok(html.indexOf("财务工作台") < html.indexOf("查看财务任务明细"));
});

test("admin status cards do not call an unavailable domain ready", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "admin",
    tasks: [
      { task_code: "provider_connected", status: "ready", action_route_code: "workspace.gateway.providers", action_enabled: true },
    ],
  });
  assert.match(html, /data-domain-status="pending"/u);
  assert.match(html, /等待状态/u);
  assert.doesNotMatch(html, /data-domain-status="pending"[\s\S]*已就绪/u);
});

test("admin and finance empty states retain honest status workbenches", () => {
  const adminHtml = renderWorkspaceTaskHome({ role_code: "admin", tasks: [] });
  assert.equal((adminHtml.match(/class="workspace-admin-domain"/gu) ?? []).length, 4);
  assert.equal((adminHtml.match(/data-domain-status="pending"/gu) ?? []).length, 4);
  assert.match(adminHtml, /等待状态/u);

  const financeHtml = renderWorkspaceTaskHome({ role_code: "finance_admin", tasks: [] });
  assert.match(financeHtml, /财务与风险/u);
  assert.match(financeHtml, /data-domain-status="pending"/u);
  assert.match(financeHtml, /查看财务任务明细/u);
});

test("completed work domains say completed instead of ready", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "admin",
    tasks: ["provider_connected", "model_catalog_ready", "first_gateway_call"].map((task_code) => ({
      task_code,
      status: "completed",
      action_route_code: null,
      action_enabled: false,
    })),
  });
  assert.match(html, /data-domain-status="completed"[\s\S]*已完成/u);
});

test("completed work domains keep an authorized review route", () => {
  const html = renderWorkspaceTaskHome({
    role_code: "admin",
    tasks: [{
      task_code: "provider_connected",
      status: "completed",
      action_route_code: "workspace.gateway.providers",
      action_enabled: true,
    }],
  });
  assert.match(html, /data-domain-status="completed"[\s\S]*data-workspace-task-route="workspace.gateway.providers">查看配置/u);
});

test("completed steps remain reviewable and only an actionable unfinished step is highlighted", () => {
  const html = renderWorkspaceTaskHome({ tasks: [
    { task_code: "provider_connected", status: "blocked", action_route_code: null, action_enabled: false },
    { task_code: "model_catalog_ready", status: "completed", action_route_code: "workspace.gateway.models", action_enabled: true },
    { task_code: "project_key_ready", status: "ready", action_route_code: "workspace.gateway.virtual-keys", action_enabled: true },
  ] });
  assert.match(html, /data-workspace-task-route="workspace.gateway.models">查看配置/u);
  assert.match(html, /class="workspace-next-action" data-workspace-task-route="workspace.gateway.virtual-keys">成员 KEY 已启用/u);
  assert.equal((html.match(/aria-current="step"/gu) ?? []).length, 1);
});

test("unavailable progress has a visible retry action", () => {
  const html = renderWorkspaceTaskHome({ status: "error" });
  assert.match(html, /接入进度加载失败/u);
  assert.match(html, /data-resource-retry>重新加载/u);
});

test("start buttons describe the destination and current step without generic open actions", () => {
  const tasks = ["provider_connected", "model_catalog_ready", "project_key_ready", "first_gateway_call"];
  const routes = ["providers", "models", "virtual-keys"];
  const html = renderWorkspaceTaskHome({ tasks: tasks.map((task_code, index) => ({ task_code,
    action_route_code: index === 3 ? "workspace.developer.sdk" : `workspace.gateway.${routes[index]}`,
    action_enabled: true, status: "ready" })) });
  for (const label of ["配置模型服务", "配置模型", "成员 KEY 已启用", "查看接入信息"]) {
    assert.ok(html.includes(`>${label}</button>`));
  }
  assert.equal((html.match(/class="workspace-task-status">当前事项/gu) ?? []).length, 1);
  assert.doesNotMatch(html, />打开<|可以处理|继续配置/u);
});

test("role-specific tasks do not treat array indexes as next-step flags", () => {
  const html = renderWorkspaceTaskHome({ tasks: ["security_policy_ready", "dlp_ready"].map((task_code) => ({
    task_code, action_route_code: "workspace.settings.profile", action_enabled: true, status: "ready",
  })) });
  assert.doesNotMatch(html, /workspace-next-action|>下一步</u);
});

test("narrowing the window closes navigation without changing desktop preferences or replacing forms", () => {
  let listener;
  let mobileResets = 0;
  const media = { matches: false,
    addEventListener: (event, callback) => { assert.equal(event, "change"); listener = callback; },
    removeEventListener: (event, callback) => { assert.equal(event, "change"); assert.equal(listener, callback); listener = null; },
  };
  const shell = { isConnected: true };
  const state = { preferences: { preference: { sidebar_collapsed: false } }, mobileSidebarCollapsed: false };
  const values = [];
  const dispose = bindConsoleSidebarViewport({ shell, state,
    windowObject: { matchMedia: (query) => { assert.equal(query, "(max-width: 820px)"); return media; } },
    applyCollapsed: (element, value) => { assert.equal(element, shell); values.push(value); },
    onCompact: () => mobileResets++,
  });
  media.matches = true;
  listener();
  assert.equal(state.mobileSidebarCollapsed, true);
  assert.equal(state.preferences.preference.sidebar_collapsed, false);
  media.matches = false;
  listener();
  state.preferences.preference.sidebar_collapsed = true;
  listener();
  assert.deepEqual(values, [true, false, true]);
  assert.equal(mobileResets, 1);
  shell.isConnected = false;
  listener();
  assert.equal(values.length, 3);
  dispose();
  assert.equal(listener, null);
});

test("workspace member KEY flow has no employee selector or login fields", () => {
  const memberAuth = { permission_codes: [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE] };
  const html = renderVirtualKeyPanel({ state: "ok", virtual_keys: [],
  }, memberAuth, { state: "ok", projects: [], applications: [] }, { state: "ok", environments: [{ environment_id: "env_1", status: "active" }] });
  assert.match(html, /<h2 id="virtual-keys-title">我的 KEY<\/h2>/u);
  assert.match(html, /placeholder="例如：开发用 KEY"/u);
  assert.doesNotMatch(html, /name="consumerId"|consumer-directory|name="(?:password|email)"/u);
  assert.doesNotMatch(html, /name="(?:password|email)"/u);
});

test("management pages do not add cross-page next-step links", () => {
  const models = renderModelCatalogPanel({ state: "ok", models: [{ capabilities: {} }] }, { state: "ok", providers: [] }, auth);
  const routes = renderModelAliasPanel({ state: "ok", model_aliases: [{}] }, { state: "ok", models: [] }, auth, { state: "ok", environments: [] });
  const keys = renderVirtualKeyPanel({ state: "ok", virtual_keys: [{}] }, auth, {}, {});
  for (const html of [models, routes, keys]) {
    assert.doesNotMatch(html, /console-flow-(?:next|related)|href="#workspace\.(?:gateway\.aliases|gateway\.virtual-keys|developer\.sdk)"/u);
  }
  assert.match(models, /<dialog class="model-create-dialog"/u);
  assert.match(routes, /<dialog class="model-alias-dialog"/u);
  assert.match(routes, /data-model-alias-create-open[\s\S]*data-model-alias-dialog/u);
  assert.doesNotMatch(routes, /<details class="model-create-disclosure">/u);
});

test("a newly added provider does not force an API KEY dialog open", () => {
  const html = renderProviderPanel({ state: "ok", setupProviderId: "p2", providers: ["p1", "p2"].map((provider_id) => ({
    provider_id, name: provider_id, status: "active", health_status: "unknown", auth_type: "bearer",
  })) }, auth);
  assert.doesNotMatch(html, /data-provider-key-auto-open/u);
  assert.match(html, /data-provider-key-open="p2"/u);
  assert.match(html, /data-provider-edit-open="p2"/u);
});

test("provider setup opens the required key dialog without advanced fields", () => {
  const html = renderProviderPanel({ state: "ok", providers: [{ provider_id: "p1", name: "DeepSeek",
    status: "active", health_status: "unknown", auth_type: "bearer" }],
  providerKeysByProviderId: { p1: { state: "ok", provider_keys: [] } } }, auth);
  assert.match(html, /data-provider-key-open="p1"[\s\S]*?>配置 API KEY<\/button>/u);
  assert.match(html, /data-provider-key-dialog/u);
  assert.match(html, /data-provider-delete-form/u);
  assert.doesNotMatch(html, /<details class="provider-maintenance"|<details class="provider-key-weight"|高级设置/u);
  assert.match(html, /name="plainValue" type="password"/u);
  assert.doesNotMatch(html, /还没有可管理的 Provider Key/u);
});

test("provider key state distinguishes loading, failure and confirmed absence", () => {
  const provider = { provider_id: "p1", name: "DeepSeek", auth_type: "bearer", health_status: "unknown" };
  const state = { state: "ok", providers: [provider] };
  const loading = renderProviderPanel(state, auth);
  assert.match(loading, /正在读取/u);
  assert.doesNotMatch(loading, /待配置密钥|0 个 Key/u);
  const failed = renderProviderPanel({ ...state, providerKeysByProviderId: { p1: { state: "error" } } }, auth);
  assert.match(failed, /密钥列表暂不可用[\s\S]*?data-resource-retry/u);
  assert.doesNotMatch(failed, /待配置密钥|0 个 Key/u);
  const unavailable = renderProviderPanel({ state: "error", message: "暂不可用" }, auth);
  assert.match(unavailable, /data-resource-retry>重试/u);
});

test("no-auth providers omit irrelevant key forms and read-only roles cannot open write setup", () => {
  const state = { state: "ok", providers: [{ provider_id: "p1", name: "Local", auth_type: "none" }] };
  const noAuth = renderProviderPanel(state, auth);
  assert.doesNotMatch(noAuth, /data-provider-key-open|data-provider-key-create-form|name="plainValue"/u);
  const readonly = renderProviderPanel({ ...state, providers: [{ ...state.providers[0], auth_type: "bearer" }] }, {
    permission_codes: [PERMISSIONS.PROVIDER_READ],
  });
  assert.doesNotMatch(readonly, /data-provider-key-open|data-provider-key-create-form|data-provider-status-form/u);
});

test("switching public forms updates refresh intent without dropping unrelated query state", () => {
  const location = { href: "http://localhost/identity.html?view=register&from=home" };
  const windowObject = { location, history: { replaceState: (_s, _t, url) => { location.href = new URL(url, location.href).href; } } };
  syncIdentityEntryView(windowObject, "login");
  assert.equal(new URL(location.href).searchParams.get("view"), "login");
  assert.equal(new URL(location.href).searchParams.get("from"), "home");
});

test("model and route lists show useful names without generated identifiers", () => {
  const model = { model_id: "model_generated_id", internal_model_id: "deepseek-chat", provider_model_id: "deepseek-chat",
    provider_code: "provider_generated_code", provider_name: "DeepSeek", status: "active", context_window: 0 };
  const html = renderModelCatalogPanel({ state: "ok", models: [model], feedback: "模型已创建" }, { state: "ok", providers: [] }, auth);
  assert.doesNotMatch(html, /<em>model_generated_id|>provider_generated_code</u);
  assert.doesNotMatch(html, />\s*(?:未设置|model_generated_id|provider_generated_code)\s*</u);
  assert.match(html, /未配置价格/u);
  assert.match(html, /role="status">模型已创建/u);
  const aliases = renderModelAliasPanel({ state: "ok", model_aliases: [{ model_alias_id: "alias_generated_id", alias: "company-default",
    default_model_id: model.model_id, default_internal_model_id: "deepseek-chat", default_provider_code: model.provider_code, status: "active" }] },
  { state: "ok", models: [model] }, auth, { state: "ok", environments: [] });
  assert.doesNotMatch(aliases, /<em>alias_generated_id|>provider_generated_code<|>active<|>Fallback</u);
  assert.match(aliases, /<em>DeepSeek<\/em>/u);
  assert.match(aliases, /1 条路由/u);
  for (const label of ["可选模型入口", "优先模型", "环境", "更新时间"]) {
    assert.ok(aliases.includes(`<span class="model-alias-field-label">${label}</span>`));
  }
});

test("route dependency failure offers recovery instead of claiming configuration is absent", () => {
  const html = renderModelAliasPanel({ state: "ok", model_aliases: [] }, { state: "error" }, auth, { state: "ok", environments: [] });
  assert.match(html, /路由所需配置加载失败/u);
  assert.match(html, /data-resource-retry/u);
  assert.doesNotMatch(html, /请先创建并启用模型/u);
});

test("organization KEY limits stay collapsed and irrelevant project placeholders stay absent", () => {
  const html = renderVirtualKeyPanel({ state: "ok", virtual_keys: [{ virtual_key_id: "key_1", name: "组织 KEY", status: "active" }] }, auth, {}, {});
  assert.match(html, /class="virtual-key-limit-open"[^>]*>调整限制/u);
  assert.match(html, /class="virtual-key-limit-dialog"/u);
  assert.doesNotMatch(html, /class="virtual-key-limit-editor"/u);
  assert.match(html, /不限速/u);
  assert.doesNotMatch(html, /未绑定项目|未绑定应用|>active</u);
});

test("tool connection comes before optional programming examples and hides placeholder models", () => {
  const metadata = { state: "ok", gateway_base_url: "https://gateway.example/v1", chat_model_alias: "company-default", chat_model_configured: true };
  const html = renderSdkExamples({ pythonState: metadata });
  assert.ok(html.indexOf("网关地址") < html.indexOf('class="sdk-developer-details"'));
  assert.match(html, /<h3>员工接入<\/h3>/u);
  assert.match(html, /员工不用填写 MetaHub 环境变量/u);
  assert.match(html, /个人 KEY/u);
  assert.match(html, /统一 MetaHub 网关网址/u);
  assert.match(html, /data-copy-code-target="tool-service-address"/u);
  assert.match(html, /模型名称/u);
  assert.match(html, /id="tool-service-address">https:\/\/gateway\.example/u);
  assert.doesNotMatch(html, /环境变量和配置文件只是 Codex、Claude Code 等客户端保存地址与 KEY 的方式/u);
  assert.match(html, /id="tool-model-name">company-default/u);
  assert.match(html, /<details class="sdk-developer-details">/u);
  const empty = renderSdkExamples({ pythonState: { ...metadata, chat_model_configured: false } });
  assert.match(empty, /尚未配置可用模型路由/u);
  assert.doesNotMatch(empty, /id="tool-model-name"/u);
});

test("tool handoff separates gateway credentials from unverified Agent compatibility", () => {
  const html = renderSdkExamples({ pythonState: {
    state: "ok", gateway_base_url: "https://gateway.example/v1", chat_model_alias: "company-default", chat_model_configured: true,
  } });
  const employeeSection = html.slice(0, html.indexOf('<details class="sdk-developer-details">'));
  const quickstart = employeeSection.slice(0, employeeSection.indexOf('<section class="tool-compatibility"'));
  assert.doesNotMatch(quickstart, /OPENAI_BASE_URL|ANTHROPIC_BASE_URL|export |baseURL|复制配置模板/u);
  assert.match(employeeSection, /<details class="tool-handoff-preview"><summary>查看接入说明/u);
  assert.match(employeeSection, /id="tool-connection-instructions">MetaHub 工具接入说明/u);
  assert.match(employeeSection, /说明不含 KEY/u);
  const guides = [...employeeSection.matchAll(/<details[^>]+data-tool-guide="([^"]+)"[^>]*>(.*?)<\/details>/gsu)];
  assert.equal(guides.length, 6);
  for (const guide of guides) assert.doesNotMatch(guide[0].split(">")[0], /\sopen(?:\s|=|$)/u);
  for (const id of ["codex", "claude-code"]) {
    const guide = guides.find((match) => match[1] === id)[2];
    assert.match(guide, /暂未支持/u);
    assert.match(guide, /客户端待联调/u);
    assert.doesNotMatch(guide, /当前支持|需选择兼容模式/u);
  }
  for (const id of ["cursor", "workbuddy", "deepseek-harness"]) {
    assert.doesNotMatch(guides.find((match) => match[1] === id)[2], /compatibility-supported/u);
  }
});

test("client addresses generate the right request paths including reverse proxy prefixes", () => {
  for (const prefix of ["", "/gateway/team"]) {
    const html = renderSdkExamples({ pythonState: {
      state: "ok", gateway_base_url: `https://gateway.example${prefix}/v1/`,
      chat_model_alias: "company-default", chat_model_configured: true,
    } });
    const claude = JSON.parse(connectionCode(html, "tool-claude-code-config"));
    const codex = connectionCode(html, "tool-codex-config");
    assert.equal(connectionCode(html, "tool-service-address"), `https://gateway.example${prefix}`);
    const codexAddress = JSON.parse(codex.match(/^base_url = (.+)$/mu)[1]);
    assert.equal(`${claude.env.ANTHROPIC_BASE_URL}/v1/messages`, `https://gateway.example${prefix}/v1/messages`);
    assert.equal(`${codexAddress}/responses`, `https://gateway.example${prefix}/v1/responses`);
    for (const tool of ["openai-chat", "codex", "cursor", "workbuddy", "deepseek-harness"]) {
      assert.equal(connectionCode(html, `tool-${tool}-address`), `https://gateway.example${prefix}/v1`);
    }
    assert.equal(connectionCode(html, "tool-claude-code-address"), claude.env.ANTHROPIC_BASE_URL);
    const handoff = connectionCode(html, "tool-connection-instructions");
    assert.ok(handoff.includes(`统一网关网址：https://gateway.example${prefix}`));
    assert.match(handoff, /个人 KEY：粘贴你在组织空间创建的个人 KEY/u);
    assert.equal(claude.env.ANTHROPIC_MODEL, "company-default");
    assert.equal(claude.env.ANTHROPIC_AUTH_TOKEN, "粘贴你的个人 KEY");
    assert.match(codex, /^wire_api = "responses"$/mu);
    assert.match(codex, /^env_key = "METAHUB_API_KEY"$/mu);
    assert.match(html, /支持自定义模型接口的工具/u);
    assert.equal(claude.env.ANTHROPIC_BASE_URL, `https://gateway.example${prefix}`);
  }
});

test("tool configuration preserves model strings without injecting markup or leaking a key", () => {
  const model = 'company"\\</code><img src=x onerror=alert(1)>';
  const html = renderSdkExamples({ pythonState: {
    state: "ok", gateway_base_url: "https://gateway.example/v1", chat_model_alias: model,
    chat_model_configured: true, secret_key: "synthetic-private-employee-key",
  } });
  assert.equal(JSON.parse(connectionCode(html, "tool-claude-code-config")).env.ANTHROPIC_MODEL, model);
  assert.equal(JSON.parse(connectionCode(html, "tool-codex-config").match(/^model = (.+)$/mu)[1]), model);
  assert.doesNotMatch(html, /<img src=x|synthetic-private-employee-key/u);
});

test("tool setup does not publish unusable configuration for missing routes or invalid addresses", () => {
  const metadata = { state: "ok", gateway_base_url: "https://gateway.example/v1", chat_model_configured: false };
  const html = renderSdkExamples({ pythonState: metadata });
  assert.doesNotMatch(html, /id="tool-(?:codex|claude-code)-config"/u);
  for (const address of [undefined, "javascript:alert(1)", "https://user:password@gateway.example/v1", "https://gateway.example/v1?api_key=private", "https://gateway.example/v1#fragment", "https://gateway.example"]) {
    const invalid = renderSdkExamples({ pythonState: { ...metadata, gateway_base_url: address } });
    const employeeSection = invalid.slice(0, invalid.indexOf('<details class="sdk-developer-details">'));
    assert.match(employeeSection, /网关地址尚未正确配置/u);
    assert.doesNotMatch(employeeSection, /data-copy-code-target|password|api_key=private/u);
  }
});

function connectionCode(html, id) {
  const encoded = html.match(new RegExp(`<code id="${id}">([\\s\\S]*?)<\\/code>`, "u"))?.[1];
  assert.notEqual(encoded, undefined, `missing ${id}`);
  return encoded.replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", '"')
    .replaceAll("&#039;", "'").replaceAll("&amp;", "&");
}

test("loading connection metadata does not expose a template with placeholder addresses", () => {
  const html = renderSdkExamples({ pythonState: { state: "loading" } });
  assert.match(html, /正在加载接入信息/u);
  assert.doesNotMatch(html, /id="tool-gateway-address"|id="tool-connection-instructions"/u);
});
