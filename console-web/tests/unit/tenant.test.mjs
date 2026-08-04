// 本单测覆盖租户信息前端能力，确保服务模式、租户名称和权限错误处理符合隔离要求。
import assert from "node:assert/strict";
import test from "node:test";

import { AuthApiError } from "../../src/lib/auth.js";
import { getCurrentTenant, updateCurrentTenant } from "../../src/lib/tenant.js";
import { renderTenantPanel } from "../../src/features/tenant/renderTenantPanel.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造具备租户设置权限的真实授权快照，避免测试按 admin 角色名隐式补权。 */
function tenantAdminContext() {
  return withPermissionSnapshot(
    { user: { email: "admin@metahub.local", role: "admin" } },
    [PERMISSIONS.TENANT_SETTINGS_READ],
  );
}

test("tenant client reads and updates current tenant with bearer token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_tenant"]]),
      async json() {
        return {
          tenant: {
            tenant_id: "tenant_1",
            slug: "default",
            name: "MetaHub Default Tenant",
            service_mode: "internal_only",
            status: "active",
          },
        };
      },
    };
  };

  const current = await getCurrentTenant({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
  });
  const updated = await updateCurrentTenant({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    name: "MetaHub Demo Tenant",
    serviceMode: "public_service",
  });

  assert.equal(current.state, "ok");
  assert.equal(updated.tenant.slug, "default");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[1].options.method, "PATCH");
  assert.equal(JSON.parse(calls[1].options.body).tenant_id, undefined);
  assert.equal(JSON.parse(calls[1].options.body).service_mode, "public_service");
});

test("tenant client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_tenant_denied",
        trace_id: "trc_tenant_denied",
      };
    },
  });

  await assert.rejects(
    () =>
      updateCurrentTenant({
        controlApiUrl: "http://control.local",
        token: "jwt-token",
        name: "Denied",
      }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_tenant_denied");
      return true;
    },
  );
});

test("renderTenantPanel displays tenant form and escapes unsafe values", () => {
  const html = renderTenantPanel(
    {
      state: "ok",
      tenant: {
        tenant_id: "tenant_1",
        slug: "<default>",
        name: "<Tenant>",
        service_mode: "public_service",
        status: "active",
        updated_at: "2026-07-07T10:00:00Z",
      },
    },
    tenantAdminContext(),
  );

  assert.match(html, /tenant-form/);
  assert.match(html, /public_service" selected/);
  assert.doesNotMatch(html, /<Tenant>/);
  assert.match(html, /&lt;Tenant&gt;/);
  assert.match(html, /admin@metahub.local/);
});

test("renderTenantPanel shows errors with request id", () => {
  const html = renderTenantPanel(
    {
      state: "error",
      message: "<script>bad()</script>",
      requestId: "req_tenant",
    },
    tenantAdminContext(),
  );

  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /req_tenant/);
});

test("renderTenantPanel fails closed when a signed-in user lacks tenant settings permission", () => {
  const html = renderTenantPanel(
    { state: "ok", tenant: { tenant_id: "tenant_1", name: "Hidden Tenant" } },
    withPermissionSnapshot(
      { user: { email: "admin@metahub.local", role: "admin" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
  );

  assert.equal(html, "");
});
