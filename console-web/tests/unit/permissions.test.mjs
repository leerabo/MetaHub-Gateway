/**
 * PRD-059 前端专项：验证权限快照不回退角色、导航指纹一致性、严格 API wire 与 PAGE-085 owner/CAS。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { AuthApiError } from "../../src/lib/auth.js";
import {
  authorizationIdentitySnapshot,
  authorizeNavigationSnapshot,
  getPermissions,
  hasPermission,
  PERMISSIONS,
  replaceRolePermissions,
} from "../../src/lib/permissions.js";
import { createRolePermissionPageState } from "../../src/features/role-permissions/rolePermissionPageState.js";
import { renderRolePermissionMatrix } from "../../src/features/role-permissions/renderRolePermissionMatrix.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { createI18n } from "../../src/i18n/messages.js";

// 固定哈希只用于构造合法矩阵和制造 revision 漂移，不承载真实授权或租户信息。
const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);

/** 构造同时含顶层和 user 镜像的真实授权摘要。 */
function authContext(overrides = {}) {
  const permissionCodes = overrides.permissionCodes ?? [
    PERMISSIONS.CONSOLE_BASIC_READ,
    PERMISSIONS.RBAC_MATRIX_MANAGE,
  ];
  const fingerprint = overrides.fingerprint ?? HASH_A;
  const revision = overrides.revision ?? 3;
  return {
    permission_codes: permissionCodes,
    permission_revision: revision,
    authorization_fingerprint: fingerprint,
    tenant: { tenant_id: "tenant_a", name: "Tenant A" },
    session: { session_id: "session_a", session_revision: 7 },
    user: {
      tenant_id: "tenant_a",
      user_id: "user_a",
      email: "admin@example.com",
      role: overrides.role ?? "admin",
      permission_codes: permissionCodes,
      permission_revision: revision,
      authorization_fingerprint: fingerprint,
    },
  };
}

/**
 * 构造有界 Permission/RolePermission 目录。
 * 返回完整六角色、同租户权限和稳定 revision/hash，供状态机主流程与整包失败关闭用例复用。
 */
function matrixPayload() {
  const permissions = [
    permission("console.basic.read", "console", "基础控制台", "read"),
    permission("rbac.matrix.manage", "rbac", "角色权限", "critical"),
    permission("audit.read", "audit", "读取审计", "read"),
    permission("audit.export", "audit", "导出审计", "critical"),
  ];
  const role = (code, index, codes, grantable = codes, immutable = false) => ({
    role_id: `role_${index}`,
    tenant_id: "tenant_a",
    code,
    name: {
      admin: "Admin",
      developer: "Developer",
      viewer: "Viewer",
      finance_admin: "FinanceAdmin",
      security_admin: "SecurityAdmin",
      ops_admin: "OpsAdmin",
    }[code],
    permission_revision: 1,
    matrix_sha256: index % 2 ? HASH_A : HASH_B,
    immutable,
    permission_codes: codes,
    grantable_permission_codes: grantable,
  });
  // 返回完整六角色目录，任何测试删项都应触发状态机整包失败关闭。
  return {
    catalog_revision: 1,
    permissions,
    roles: [
      role("admin", 0, permissions.map((item) => item.code), permissions.map((item) => item.code), true),
      role("developer", 1, ["console.basic.read"]),
      role("viewer", 2, ["console.basic.read"]),
      role("finance_admin", 3, ["console.basic.read"]),
      role("security_admin", 4, ["console.basic.read", "audit.read", "audit.export"]),
      role("ops_admin", 5, ["console.basic.read"]),
    ],
    request_id: "req_matrix",
    trace_id: "trc_matrix",
  };
}

/**
 * 构造一条租户权限 DTO。
 * 参数是规范 code、分类、名称和风险枚举；返回值显式绑定 tenant_a，便于验证 owner 隔离。
 */
function permission(code, category, name, riskLevel) {
  // permission_id 与 tenant_id 显式绑定，便于测试跨租户和重复目录拒绝路径。
  return {
    permission_id: `perm_${code.replaceAll(".", "_")}`,
    tenant_id: "tenant_a",
    code,
    category,
    name,
    description: `${name}的安全说明。`,
    risk_level: riskLevel,
    status: "active",
  };
}

/**
 * 构造 PAGE-085 完整 owner。
 * 参数提供认证上下文与刷新代次；返回值只含授权指纹、tenant 和导航，不含 Token。
 */
function pageOwner(context = authContext(), refreshGeneration = 1) {
  // owner 使用后端授权指纹而非角色名，确保迟到响应不能跨身份或跨租户生效。
  return {
    identity: authorizationIdentitySnapshot(context, PERMISSIONS.RBAC_MATRIX_MANAGE),
    tenantId: context.user.tenant_id,
    navigation: "role-permissions",
    refreshGeneration,
  };
}

test("Workspace matrix accepts only an authoritative matching scope and same-tenant rows", () => {
  const owner = { identity: "workspace-session-a", workspaceId: "workspace_a", navigation: "role-permissions", refreshGeneration: 1 };
  const payload = { ...matrixPayload(), workspace_id: "workspace_a", tenant_id: "tenant_a" };
  for (const invalid of [
    matrixPayload(),
    { ...payload, workspace_id: "workspace_b" },
    { ...payload, tenant_id: "tenant_b" },
    { ...payload, roles: payload.roles.map((role, index) => index ? role : { ...role, tenant_id: "tenant_b" }) },
  ]) {
    const state = createRolePermissionPageState();
    assert.equal(state.acceptLoad(state.beginLoad(owner), invalid, owner), false);
    assert.equal(state.snapshot().state, "error");
  }
  const state = createRolePermissionPageState();
  assert.equal(state.acceptLoad(state.beginLoad(owner), payload, owner), true);
  const late = state.beginLoad(owner);
  assert.equal(state.acceptLoad(late, payload, { ...owner, workspaceId: "workspace_b" }), false);
  state.setPermission("role_4", "audit.export", false);
  const mutation = state.beginMutation(owner);
  assert.ok(mutation);
  const role = { ...payload.roles[4], permission_codes: ["console.basic.read", "audit.read"], permission_revision: 2 };
  assert.equal(state.acceptMutation(mutation, {
    role, workspace_id: "workspace_b", tenant_id: "tenant_a", changed: true, event_id: "event_a",
  }, owner), false);
  assert.equal(state.snapshot().dirty, true);
});

test("permission snapshot is exact and never falls back to role", () => {
  const context = authContext();
  assert.equal(hasPermission(context, PERMISSIONS.RBAC_MATRIX_MANAGE), true);
  assert.match(authorizationIdentitySnapshot(context, PERMISSIONS.RBAC_MATRIX_MANAGE), /rbac\.matrix\.manage/);
  assert.equal(hasPermission({ user: { role: "admin" } }, PERMISSIONS.RBAC_MATRIX_MANAGE), false);
  assert.equal(hasPermission({
    ...context,
    user: { ...context.user, permission_codes: [PERMISSIONS.CONSOLE_BASIC_READ] },
  }, PERMISSIONS.RBAC_MATRIX_MANAGE), false);
});

test("PRD-067 prompt template permissions remain three independent product codes", () => {
  assert.equal(PERMISSIONS.PROMPT_TEMPLATE_READ, "prompt_template.read");
  assert.equal(PERMISSIONS.PROMPT_TEMPLATE_WRITE, "prompt_template.write");
  assert.equal(PERMISSIONS.PROMPT_TEMPLATE_USE, "prompt_template.use");
  const context = authContext({
    permissionCodes: [PERMISSIONS.CONSOLE_BASIC_READ, PERMISSIONS.PROMPT_TEMPLATE_USE],
  });
  assert.equal(hasPermission(context, PERMISSIONS.PROMPT_TEMPLATE_USE), true);
  assert.equal(hasPermission(context, PERMISSIONS.PROMPT_TEMPLATE_READ), false);
  assert.equal(hasPermission(context, PERMISSIONS.PROMPT_TEMPLATE_WRITE), false);
});

test("navigation requires matching permission revision fingerprint and required permissions", () => {
  const context = authContext();
  const navigation = authorizeNavigationSnapshot({
    permission_codes: context.permission_codes,
    permission_revision: context.permission_revision,
    authorization_fingerprint: context.authorization_fingerprint,
    navigation_items: [
      { id: "overview", visible: true, required_permissions: [PERMISSIONS.CONSOLE_BASIC_READ] },
      { id: "role-permissions", visible: true, required_permissions: [PERMISSIONS.RBAC_MATRIX_MANAGE] },
      { id: "providers", visible: true, required_permissions: [PERMISSIONS.PROVIDER_READ] },
    ],
  }, context);
  assert.deepEqual(navigation.navigation_items.map((item) => item.id), ["overview", "role-permissions"]);
  assert.throws(
    () => authorizeNavigationSnapshot({ ...navigation, authorization_fingerprint: HASH_B }, context),
    (error) => error instanceof AuthApiError && error.code === "rbac.matrix_integrity_failed",
  );
});

test("permissions client sends no tenant or role and keeps stable PATCH request id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      headers: { get(name) { return name === "ETag" ? `"${HASH_A}"` : null; } },
      async json() { return matrixPayload(); },
    };
  };
  await getPermissions({ controlApiUrl: "http://control.local", token: "jwt", requestId: "req_get" });
  await replaceRolePermissions({
    controlApiUrl: "http://control.local",
    token: "jwt",
    roleId: "role_4",
    expectedRevision: 1,
    permissionCodes: ["audit.read", "console.basic.read"],
    requestId: "req_patch_stable",
  });
  assert.equal(calls[0].url, "http://control.local/permissions");
  assert.equal(calls[1].url, "http://control.local/roles/role_4/permissions");
  assert.equal(calls[1].options.headers["X-Request-ID"], "req_patch_stable");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    expected_revision: 1,
    permission_codes: ["audit.read", "console.basic.read"],
  });
  assert.doesNotMatch(calls[1].options.body, /tenant|actor|role_code|fingerprint/i);
});

test("PAGE-085 state preserves dirty draft, grantable ceiling and network retry request id", () => {
  let sequence = 0;
  const state = createRolePermissionPageState({ requestIdFactory: () => `req_patch_${++sequence}` });
  const owner = pageOwner();
  const load = state.beginLoad(owner);
  assert.equal(state.acceptLoad(load, matrixPayload(), owner), true);
  assert.equal(state.selectRole("role_4"), true);
  assert.equal(state.setPermission("role_4", "audit.export", false), true);
  assert.equal(state.setPermission("role_4", "rbac.matrix.manage", true), false);
  const first = state.beginMutation(owner);
  assert.equal(first.requestId, "req_patch_1");
  assert.equal(state.rejectMutation(first, new TypeError("network failed"), owner), true);
  const retry = state.beginMutation(owner);
  assert.equal(retry.requestId, "req_patch_1");
  assert.equal(state.rejectMutation(retry, {
    status: 409,
    code: "rbac.revision_conflict",
    message: "revision conflict",
    requestId: "req_patch_1",
  }, owner), true);
  assert.equal(state.snapshot().dirty, true);
  assert.equal(state.snapshot().serverChanged, true);
  assert.equal(state.beginMutation(owner), null);
  assert.equal(state.discardDraft(), true);
  assert.equal(state.snapshot().dirty, false);
});

test("PAGE-085 rejects cross-tenant matrix and late owner", () => {
  const state = createRolePermissionPageState();
  const owner = pageOwner();
  const first = state.beginLoad(owner);
  assert.equal(state.acceptLoad(first, matrixPayload(), { ...owner, refreshGeneration: 2 }), false);
  const second = state.beginLoad({ ...owner, refreshGeneration: 2 });
  const crossTenant = matrixPayload();
  crossTenant.permissions[0] = { ...crossTenant.permissions[0], tenant_id: "tenant_b" };
  assert.equal(state.acceptLoad(second, crossTenant, { ...owner, refreshGeneration: 2 }), false);
  assert.equal(state.snapshot().state, "error");
  assert.equal(state.snapshot().loadError.code, "rbac.matrix_integrity_failed");
});

test("PAGE-085 rejects duplicate identities and incomplete immutable admin", () => {
  const variants = [];
  const duplicatePermission = matrixPayload();
  duplicatePermission.permissions[1] = {
    ...duplicatePermission.permissions[1],
    permission_id: duplicatePermission.permissions[0].permission_id,
  };
  variants.push(duplicatePermission);
  const duplicateRole = matrixPayload();
  duplicateRole.roles[1] = {
    ...duplicateRole.roles[1],
    role_id: duplicateRole.roles[0].role_id,
  };
  variants.push(duplicateRole);
  const incompleteAdmin = matrixPayload();
  incompleteAdmin.roles[0] = {
    ...incompleteAdmin.roles[0],
    permission_codes: incompleteAdmin.roles[0].permission_codes.slice(1),
  };
  variants.push(incompleteAdmin);

  for (const [index, payload] of variants.entries()) {
    const state = createRolePermissionPageState();
    const owner = pageOwner(authContext(), index + 1);
    const operation = state.beginLoad(owner);
    assert.equal(state.acceptLoad(operation, payload, owner), false);
    assert.equal(state.snapshot().loadError.code, "rbac.matrix_integrity_failed");
  }
});

test("PAGE-085 accepts only a PATCH response matching submitted set and revision semantics", () => {
  // 每次调用都创建独立矩阵、草稿和 CAS ticket，避免成功分支污染异常分支。
  const buildMutation = () => {
    const state = createRolePermissionPageState();
    const owner = pageOwner();
    assert.equal(state.acceptLoad(state.beginLoad(owner), matrixPayload(), owner), true);
    assert.equal(state.setPermission("role_4", "audit.export", false), true);
    // 只返回安全状态机对象和冻结 ticket，不暴露 Token 或完整认证响应。
    return { state, owner, operation: state.beginMutation(owner) };
  };
  const valid = buildMutation();
  const validRole = {
    ...matrixPayload().roles[4],
    permission_revision: 2,
    matrix_sha256: HASH_B,
    permission_codes: valid.operation.permissionCodes,
  };
  assert.equal(valid.state.acceptMutation(valid.operation, {
    role: validRole,
    changed: true,
    event_id: "rpevt_valid",
    request_id: "req_patch_result",
    trace_id: "trc_patch_result",
  }, valid.owner), true);
  assert.equal(valid.state.snapshot().dirty, false);

  for (const [name, buildInvalidResponse] of [
    ["permission set", (role, payload) => ({
      ...payload,
      role: { ...role, permission_codes: ["console.basic.read"] },
    })],
    ["revision", (role, payload) => ({
      ...payload,
      role: { ...role, permission_revision: 1 },
    })],
    ["changed flag", (role, payload) => ({
      ...payload,
      role,
      changed: false,
      event_id: null,
    })],
  ]) {
    const current = buildMutation();
    const role = {
      ...matrixPayload().roles[4],
      permission_revision: 2,
      matrix_sha256: HASH_B,
      permission_codes: current.operation.permissionCodes,
    };
    const payload = {
      role,
      changed: true,
      event_id: "rpevt_valid",
      request_id: "req_patch_result",
      trace_id: "trc_patch_result",
    };
    const invalid = buildInvalidResponse(role, payload);
    assert.equal(current.state.acceptMutation(
      current.operation,
      invalid,
      current.owner,
    ), false, name);
    assert.equal(current.state.snapshot().dirty, true);
    assert.equal(current.state.snapshot().actionError.code, "rbac.matrix_integrity_failed");
  }
});

test("PAGE-085 renderer uses permission DTO for locked and grantable DOM", () => {
  const state = createRolePermissionPageState();
  const owner = pageOwner();
  const load = state.beginLoad(owner);
  state.acceptLoad(load, matrixPayload(), owner);
  const context = authContext();
  const html = renderRolePermissionMatrix(state.snapshot(), context);
  assert.match(html, /role-permission-matrix/);
  assert.match(html, /role-permission-tabs/);
  assert.match(html, /FinanceAdmin/);
  assert.match(html, /data-permission-code="audit\.export"/);
  assert.doesNotMatch(html, /<code>audit\.export<\/code>/);
  assert.match(html, /Admin · 锁定/);
  assert.match(html, /按权限分组查看/);
  assert.match(html, /role-permission-category-help/);
  assert.doesNotMatch(html, /authorization_fingerprint|tenant_a|session_a/);
  assert.equal(renderRolePermissionMatrix(state.snapshot(), authContext({ permissionCodes: [PERMISSIONS.CONSOLE_BASIC_READ] })), "");
});

test("PAGE-085 defaults to one-role editing and exposes full comparison explicitly", () => {
  const state = createRolePermissionPageState();
  const owner = pageOwner();
  state.acceptLoad(state.beginLoad(owner), matrixPayload(), owner);
  assert.equal(state.snapshot().viewMode, "role");
  const focused = renderRolePermissionMatrix(state.snapshot(), authContext());
  assert.match(focused, /data-rbac-view-mode="role"/);
  assert.match(focused, /对比全部角色（/);
  assert.match(focused, /role-permission-focus/);
  assert.doesNotMatch(focused, /data-rbac-view-mode="all"[^>]*aria-pressed="true"/);
  state.setViewMode("all");
  assert.equal(state.snapshot().viewMode, "all");
  const comparison = renderRolePermissionMatrix(state.snapshot(), authContext());
  assert.match(comparison, /data-rbac-view-mode="all"[^>]*aria-pressed="true"/);
  assert.match(comparison, /role-permission-matrix/);
});

test("PAGE-085 groups the current role by responsibility so the first scan stays short", () => {
  const state = createRolePermissionPageState();
  const owner = pageOwner();
  state.acceptLoad(state.beginLoad(owner), matrixPayload(), owner);
  const html = renderRolePermissionMatrix(state.snapshot(), authContext());
  const focusStart = html.indexOf('class="role-permission-focus"');
  const compareStart = html.indexOf('class="role-permission-compare"');
  const focusHtml = html.slice(focusStart, compareStart > focusStart ? compareStart : undefined);
  const groups = [...focusHtml.matchAll(/<details class="role-permission-group"([^>]*)>/gu)];
  assert.ok(groups.length >= 2);
  assert.match(groups[0][1], / open/u);
  assert.doesNotMatch(groups.slice(1).map((match) => match[1]).join(" "), / open/u);
  assert.match(html, /<summary><span>/u);
  assert.match(html, /项权限/u);
});

test("PAGE-085 English projection does not mix Chinese matrix chrome", () => {
  const state = createRolePermissionPageState();
  const owner = pageOwner();
  const payload = matrixPayload();
  payload.roles[0].name = "管理员";
  state.acceptLoad(state.beginLoad(owner), payload, owner);
  const html = renderRolePermissionMatrix(state.snapshot(), authContext(), createI18n("en-US"));
  assert.match(html, /Roles and permissions/);
  assert.match(html, /Administrator/);
  assert.match(html, /Compare all roles/);
  assert.doesNotMatch(html, /角色权限|权限目录|对比全部角色|当前角色|未授权|已授权|锁定/u);
});

test("PAGE-085 save commands remain after the matrix instead of covering editable cells", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  const savebar = css.slice(css.indexOf(".role-permission-savebar {"), css.indexOf(".role-permission-savebar > div:first-child"));
  assert.doesNotMatch(savebar, /position:\s*sticky|bottom:\s*0|z-index:\s*3/u);
});

test("PAGE-085 is wired into the authorized shell and navigation failures stay closed", async () => {
  const context = authContext();
  const state = createRolePermissionPageState();
  const owner = pageOwner(context);
  state.acceptLoad(state.beginLoad(owner), matrixPayload(), owner);
  const shellState = {
    state: "ok",
    activeNavigationItemId: "role-permissions",
    navigation: {
      active_navigation_item_id: "role-permissions",
      navigation_items: [{
        id: "role-permissions",
        target_section: "role-permissions",
        group: "tenant-governance",
        group_label: "租户治理",
        label: "角色权限",
        description: "六角色权限矩阵",
        visible: true,
      }],
    },
  };
  const authorized = { innerHTML: "" };
  renderDeveloperStart(authorized, {
    authContext: context,
    consoleShellState: shellState,
    rolePermissionState: state.snapshot(),
  });
  assert.match(authorized.innerHTML, /role-permission-matrix/);
  assert.match(authorized.innerHTML, /data-console-nav="role-permissions"/);

  const denied = { innerHTML: "" };
  renderDeveloperStart(denied, {
    authContext: context,
    consoleShellState: { state: "error", error: new Error("navigation unavailable") },
  });
  assert.match(denied.innerHTML, /authorization-unavailable/);
  assert.doesNotMatch(denied.innerHTML, /role-permission-matrix/);

  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(mainSource, /case "role-permissions":[\s\S]*loadRolePermissionState\(consoleShellState, refreshId\)/);
  assert.match(mainSource, /bindRolePermissionInteractions\(consoleShellState\)/);
  assert.match(mainSource, /clearRolePermissionStateOnIdentityChange/);
});
