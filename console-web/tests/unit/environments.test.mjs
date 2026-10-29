// 本单测覆盖 Environment 客户端、管理视图、只读权限和统一错误反馈。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { renderEnvironmentPanel } from "../../src/features/environments/renderEnvironmentPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { createEnvironment, listEnvironments, updateEnvironment } from "../../src/lib/environments.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造环境页面的显式读写授权快照。 */
function environmentContext(permissionCodes, role = "ops_admin", platformRoles = []) {
  return withPermissionSnapshot({ user: { role },
    platform_context: { operator: { roles: platformRoles } },
  }, permissionCodes);
}

// 主应用源码断言固定 viewer 预取边界，避免隐藏面板后仍在每轮刷新触发后端 403。
const mainSource = readFileSync(new URL("../../src/app/main.js", import.meta.url), "utf8");

const environmentPayload = {
  environments: [
    {
      environment_id: "env_dev",
      tenant_id: "tenant_1",
      code: "dev",
      name: "开发环境",
      description: "日常联调",
      status: "active",
      virtual_key_count: 2,
      model_alias_count: 1,
      budget_rule_count: 1,
      created_at: "2026-07-11T00:00:00Z",
      updated_at: "2026-07-11T00:00:00Z",
    },
  ],
  request_id: "req_environments",
  trace_id: "trc_environments",
};

test("environment client keeps tenant out and sends stable environment id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map(),
      async json() {
        // 按方法返回最小合法响应，使断言聚焦 URL、权限头和安全请求体。
        if (options.method === "POST") return { environment: environmentPayload.environments[0] };
        if (options.method === "PATCH") return { environment: { ...environmentPayload.environments[0], status: "disabled" } };
        return environmentPayload;
      },
    };
  };

  await listEnvironments({ controlApiUrl: "http://control.local", token: "jwt", status: "active" });
  await createEnvironment({
    controlApiUrl: "http://control.local",
    token: "jwt",
    code: "test",
    name: "测试环境",
    description: "发布验证",
  });
  await updateEnvironment({
    controlApiUrl: "http://control.local",
    token: "jwt",
    environmentId: "env_test/id",
    status: "disabled",
  });

  assert.match(calls[0].url, /status=active/);
  assert.doesNotMatch(calls[0].url, /tenant/);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    code: "test",
    name: "测试环境",
    description: "发布验证",
  });
  assert.match(calls[2].url, /env_test%2Fid$/);
  assert.deepEqual(JSON.parse(calls[2].options.body), { status: "disabled" });
});

test("environment client preserves unified permission error", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map(),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_environment_denied",
        trace_id: "trc_environment_denied",
      };
    },
  });

  await assert.rejects(
    () => listEnvironments({ controlApiUrl: "http://control.local", token: "jwt" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_environment_denied");
      return true;
    },
  );
});

test("environment panel renders authorized platform controls and escapes tenant data", () => {
  const html = renderEnvironmentPanel(
    {
      state: "ok",
      ...environmentPayload,
      environments: [
        {
          ...environmentPayload.environments[0],
          name: "开发<script>",
          description: "<unsafe>",
        },
      ],
    },
    environmentContext(
      [PERMISSIONS.ENVIRONMENT_READ, PERMISSIONS.ENVIRONMENT_WRITE],
      "admin",
      ["platform_admin"],
    ),
  );

  assert.match(html, /create-environment-form/);
  assert.match(html, /data-environment-status-form/);
  assert.match(html, /2/);
  assert.match(html, /&lt;unsafe&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("environment panel is readonly for a platform reader and hidden from workspace-only identities", () => {
  const readerHtml = renderEnvironmentPanel(
    { state: "ok", ...environmentPayload },
    environmentContext([PERMISSIONS.ENVIRONMENT_READ], "ops_admin", ["platform_ops"]),
  );
  const viewerHtml = renderEnvironmentPanel(
    { state: "ok", ...environmentPayload },
    environmentContext([PERMISSIONS.CONSOLE_BASIC_READ], "viewer"),
  );

  assert.match(readerHtml, /开发环境/);
  assert.doesNotMatch(readerHtml, /create-environment-form/);
  assert.doesNotMatch(readerHtml, /data-environment-status-form/);
  assert.equal(viewerHtml, "");
  for (const role of ["owner", "admin", "developer"]) {
    assert.equal(renderEnvironmentPanel({ state: "ok", ...environmentPayload },
      environmentContext([PERMISSIONS.ENVIRONMENT_READ, PERMISSIONS.ENVIRONMENT_WRITE], role)), "");
  }
  assert.equal(renderEnvironmentPanel({ state: "ok", ...environmentPayload },
    environmentContext([PERMISSIONS.ENVIRONMENT_WRITE], "ops_admin", ["platform_admin"])), "");
});

test("environment loader does not prefetch without environment read permission", () => {
  assert.match(
    mainSource,
    /async function loadEnvironmentState\(\)\s*\{\s*return loadResourceState\("environments", \[PERMISSIONS\.ENVIRONMENT_READ\]/,
  );
});
