// 本单测覆盖开发者快速开始页：客户端不得传 tenant_id，示例只使用 Key 占位并且页面必须转义后端文本。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderDeveloperQuickstart } from "../../src/features/developer-quickstart/renderDeveloperQuickstart.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { getDeveloperQuickstartData } from "../../src/lib/developerQuickstart.js";

const quickstartState = {
  state: "ok",
  tenant_id: "tenant_1",
  gateway_base_url: "http://gateway.local/v1",
  model_alias: "company-default",
  virtual_key_placeholder: "METAHUB_VIRTUAL_KEY",
  has_active_virtual_key: true,
  virtual_key_prefix: "mhv_dev_abcd",
  request_id: "req_quickstart",
  trace_id: "trc_quickstart",
  steps: [
    {
      id: "prepare-key",
      order: 1,
      title: "准备 <Key>",
      description: "使用环境变量，不展示明文 Key。",
      status: "ready",
      action_label: "查看 Key",
      target_hash: "#virtual-keys",
    },
  ],
  examples: [
    {
      id: "python-openai",
      language: "python",
      title: "Python",
      description: "Python 示例",
      install_command: "pip install openai",
      code: 'api_key=os.environ["METAHUB_VIRTUAL_KEY"]\nmodel="company-default"',
      copy_label: "复制 Python",
    },
    {
      id: "curl-chat",
      language: "curl",
      title: "curl",
      description: "curl 示例",
      install_command: null,
      code: "Authorization: Bearer ${METAHUB_VIRTUAL_KEY}",
      copy_label: "复制 curl",
    },
  ],
  common_errors: [
    {
      code: "gateway.auth_invalid",
      title: "Virtual Key 无效",
      cause: "<unsafe>",
      fix: "重新创建 dev Key",
      link_target: "#virtual-keys",
    },
  ],
};

test("developer quickstart client sends request without tenant id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_quickstart"]]),
      async json() {
        return { ...quickstartState };
      },
    };
  };

  const result = await getDeveloperQuickstartData({ controlApiUrl: "http://control.local", token: "jwt-token" });

  assert.equal(calls[0].url, "http://control.local/developer/quickstart-data");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(JSON.stringify(calls).includes("tenant_id"), false);
  assert.equal(result.gateway_base_url, "http://gateway.local/v1");
});

test("developer quickstart client maps unified errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_denied",
        trace_id: "trc_denied",
      };
    },
  });

  await assert.rejects(
    () => getDeveloperQuickstartData({ controlApiUrl: "http://control.local", token: "viewer-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_denied");
      return true;
    },
  );
});

test("renderDeveloperQuickstart escapes text and keeps secret placeholders", () => {
  const html = renderDeveloperQuickstart(quickstartState);

  assert.match(html, /5分钟快速接入/);
  assert.match(html, /http:\/\/gateway.local\/v1/);
  assert.match(html, /company-default/);
  assert.match(html, /METAHUB_VIRTUAL_KEY/);
  assert.match(html, /data-copy-code-target="quickstart-code-python-openai"/);
  assert.doesNotMatch(html, /mhv_dev_seed_virtual_key/);
  assert.doesNotMatch(html, /<unsafe>/);
  assert.match(html, /&lt;unsafe&gt;/);
  assert.doesNotMatch(html, /准备 <Key>/);
  assert.match(html, /准备 &lt;Key&gt;/);
});

test("renderDeveloperStart migrates the quickstart route to the Workspace task home", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: {
      user: { display_name: "Dev", email: "dev@example.com", role: "developer", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-09T12:00:00Z" },
    },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "quickstart",
        navigation_items: [
          {
            id: "quickstart",
            label: "快速开始",
            group: "开发者",
            target_section: "quickstart",
            description: "Quickstart",
            empty_state_hint: "Quickstart",
            order: 10,
          },
        ],
      },
    },
    quickstartState,
  });

  assert.match(container.innerHTML, /class="workspace-task-home"/);
  assert.match(container.innerHTML, /正在加载接入进度/);
  assert.doesNotMatch(container.innerHTML, /5分钟快速接入/);
  assert.doesNotMatch(container.innerHTML, /quickstart-code-python-openai/);
  assert.doesNotMatch(container.innerHTML, /http:\/\/gateway\.local/);
  assert.doesNotMatch(container.innerHTML, /API 文档 · OpenAI-Compatible/);
});

test("overview gives a new organization an explicit first action", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: {
      user: { display_name: "Owner", email: "owner@example.com", role: "owner", tenant_id: "tenant_1" },
      tenant: { name: "新组织" },
      permission_codes: ["workspace.console.overview.read"],
    },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "overview",
        role_code: "owner",
        navigation_items: [
          { id: "overview", label: "概览", group: "空间", target_section: "overview", order: 1 },
          { id: "quickstart", label: "开始使用", group: "空间", target_section: "quickstart", order: 2 },
        ],
      },
    },
    workspaceTaskHomeState: {
      role_code: "owner",
      source_freshness: "fresh",
      tasks: [
        { task_code: "provider_connected", status: "ready", action_enabled: true, action_route_code: "workspace.gateway.providers" },
        { task_code: "model_catalog_ready", status: "blocked", action_enabled: false, action_route_code: "workspace.gateway.models" },
        { task_code: "project_key_ready", status: "blocked", action_enabled: false, action_route_code: "workspace.gateway.virtual-keys" },
      ],
    },
    workspaceConsoleExperienceState: {
      status: "ready",
      overview: { status: "ready", sections: [] },
    },
  });

  assert.match(container.innerHTML, /配置模型接入/u);
  assert.doesNotMatch(container.innerHTML, /再交给员工使用|最后让成员.*个人 KEY/u);
  assert.match(container.innerHTML, /当前事项/u);
  assert.match(container.innerHTML, /配置模型服务/u);
  assert.match(container.innerHTML, /data-workspace-task-route="workspace.gateway.providers"/u);
});

test("completed organization setup removes the wizard from overview and keeps information cards", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: {
      user: { display_name: "Owner", email: "owner@example.com", role: "owner", tenant_id: "tenant_1" },
      tenant: { name: "组织" },
      permission_codes: ["workspace.console.overview.read"],
    },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "overview",
        role_code: "owner",
        navigation_items: [
          { id: "overview", label: "概览", group: "空间", target_section: "overview", order: 1 },
          { id: "quickstart", label: "开始使用", group: "空间", target_section: "quickstart", order: 2 },
        ],
      },
    },
    workspaceTaskHomeState: {
      role_code: "owner",
      source_freshness: "fresh",
      tasks: [
        { task_code: "provider_connected", status: "completed", source_freshness: "fresh", action_enabled: true, action_route_code: "workspace.gateway.providers" },
        { task_code: "model_catalog_ready", status: "completed", source_freshness: "fresh", action_enabled: true, action_route_code: "workspace.gateway.models" },
      ],
    },
    workspaceConsoleExperienceState: {
      status: "ready",
      overview: {
        status: "ready",
        sections: [{ source_code: "role_tasks", freshness: "fresh", metrics: [{ metric_code: "role_tasks.records", value: 3, state_code: "ready" }] }],
      },
    },
  });

  assert.doesNotMatch(container.innerHTML, /workspace-first-use-panel/u);
  assert.doesNotMatch(container.innerHTML, /组织接入进度/u);
  assert.match(container.innerHTML, /workspace-console-experience-panel/u);
  assert.match(container.innerHTML, /成员人数<\/span><strong>3<\/strong>/u);
});
