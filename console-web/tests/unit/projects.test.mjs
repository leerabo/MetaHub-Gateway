// 本单测覆盖 Project/Application 控制台能力，确保 API 不提交 tenant_id，页面区分管理员写入和开发者只读。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderProjectApplicationPanel } from "../../src/features/projects/renderProjectApplicationPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createApplication,
  createProject,
  listApplications,
  listProjects,
  updateApplication,
  updateProject,
} from "../../src/lib/projects.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const projectPayload = {
  projects: [
    {
      project_id: "prj_1",
      tenant_id: "tenant_1",
      project_code: "billing",
      name: "账务核心",
      description: "成本归因",
      owner_user_id: null,
      owner_display_name: null,
      status: "active",
      application_count: 1,
      virtual_key_count: 1,
      created_at: "2026-07-09T08:00:00Z",
      updated_at: "2026-07-09T08:00:00Z",
    },
  ],
  request_id: "req_projects",
  trace_id: "trc_projects",
};

const applicationPayload = {
  applications: [
    {
      application_id: "app_1",
      tenant_id: "tenant_1",
      project_id: "prj_1",
      project_code: "billing",
      project_name: "账务核心",
      application_code: "api",
      name: "账务 API",
      description: "线上入口",
      status: "active",
      virtual_key_count: 1,
      created_at: "2026-07-09T08:01:00Z",
      updated_at: "2026-07-09T08:01:00Z",
    },
  ],
  request_id: "req_apps",
  trace_id: "trc_apps",
};

test("projects client lists creates and updates without tenant id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_projects"]]),
      async json() {
        if (String(url).includes("/applications") && options.method !== "PATCH") {
          return applicationPayload;
        }
        if (String(url).includes("/applications") && options.method === "PATCH") {
          return { application: applicationPayload.applications[0], request_id: "req_update_app" };
        }
        if (options.method === "POST" || options.method === "PATCH") {
          return { project: projectPayload.projects[0], request_id: "req_write_project" };
        }
        return projectPayload;
      },
    };
  };

  const projects = await listProjects({ controlApiUrl: "http://control.local", token: "jwt-token", status: "all" });
  const applications = await listApplications({ controlApiUrl: "http://control.local", token: "jwt-token", projectId: "prj_1" });
  await createProject({ controlApiUrl: "http://control.local", token: "jwt-token", projectCode: "billing", name: "账务核心" });
  await updateProject({ controlApiUrl: "http://control.local", token: "jwt-token", projectId: "prj_1", status: "disabled" });
  await createApplication({ controlApiUrl: "http://control.local", token: "jwt-token", projectId: "prj_1", applicationCode: "api", name: "账务 API" });
  await updateApplication({ controlApiUrl: "http://control.local", token: "jwt-token", applicationId: "app_1", status: "disabled" });

  assert.equal(projects.projects[0].project_code, "billing");
  assert.equal(applications.applications[0].application_code, "api");
  assert.match(calls[0].url, /status=all/);
  assert.match(calls[1].url, /project_id=prj_1/);
  assert.equal(calls[2].options.method, "POST");
  assert.equal(JSON.parse(calls[2].options.body).tenant_id, undefined);
  assert.equal(JSON.parse(calls[3].options.body).status, "disabled");
  assert.equal(JSON.parse(calls[4].options.body).project_id, "prj_1");
  assert.equal(JSON.parse(calls[5].options.body).status, "disabled");
});

test("projects client returns structured errors", async () => {
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
    () => listProjects({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 403);
      assert.equal(error.requestId, "req_denied");
      return true;
    },
  );
});

test("renderProjectApplicationPanel shows admin forms and developer readonly state", () => {
  const state = { state: "ok", projects: projectPayload.projects, applications: applicationPayload.applications, feedback: "项目已创建" };
  const adminHtml = renderProjectApplicationPanel(
    state,
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.PROJECT_READ, PERMISSIONS.PROJECT_WRITE],
    ),
  );
  const developerHtml = renderProjectApplicationPanel(
    state,
    withPermissionSnapshot({ user: { role: "developer" } }, [PERMISSIONS.PROJECT_READ]),
  );
  const viewerHtml = renderProjectApplicationPanel(
    state,
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );

  assert.match(adminHtml, /create-project-form/);
  assert.match(adminHtml, /create-application-form/);
  assert.match(adminHtml, /账务核心/);
  assert.match(adminHtml, /账务 API/);
  assert.match(adminHtml, /项目已创建/);
  assert.match(developerHtml, /当前角色可查看项目与应用归属/);
  assert.doesNotMatch(developerHtml, /create-project-form/);
  assert.equal(viewerHtml, "");
});

test("renderDeveloperStart renders projects active navigation", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: withPermissionSnapshot({
      user: { display_name: "Admin", email: "admin@example.com", role: "admin", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-09T12:00:00Z" },
    }, [PERMISSIONS.PROJECT_READ, PERMISSIONS.PROJECT_WRITE]),
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "projects",
        navigation_items: [
          {
            id: "projects",
            label: "项目与应用",
            group: "调用与成本",
            target_section: "projects",
            description: "Projects",
            empty_state_hint: "Add project",
            order: 65,
          },
        ],
      },
    },
    projectState: { state: "ok", projects: projectPayload.projects, applications: applicationPayload.applications },
  });

  assert.match(container.innerHTML, /projects-panel/);
  assert.match(container.innerHTML, /账务核心/);
});
