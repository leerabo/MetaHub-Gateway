// 本单测覆盖 PRD-034 控制台审计日志，重点验证权限入口、租户参数边界、摘要转义和移动字段语义。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  auditExportIdentitySnapshot,
  createAuditExportLifecycle,
  renderAuditLogPanel,
} from "../../src/features/audit-logs/renderAuditLogPanel.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createAuditExportRequestId,
  exportAuditLogs,
  listAuditLogs,
  triggerAuditCsvDownload,
} from "../../src/lib/auditLogs.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const longRequestId = "req_prd034_" + "full_identifier_".repeat(12);
const auditPayload = {
  audit_logs: [
    {
      audit_log_id: "aud_prd034_1",
      tenant_id: "tenant_1",
      actor_user_id: "user_prd034_admin_identifier_that_must_remain_complete",
      actor_email: "admin@example.com",
      actor_role: "admin",
      action: "provider_key.updated",
      object_type: "provider_key",
      object_id: "provider_key_prd034_identifier_that_must_remain_complete",
      object_name: "生产 Key / sk-abcd",
      before_summary: { status: "active", name: "<script>unsafe</script>" },
      after_summary: { status: "disabled", name: "安全名称" },
      changed_fields: ["status"],
      client_ip: "127.0.0.1",
      user_agent: "MetaHub Console/PRD-034",
      request_id: longRequestId,
      trace_id: "trc_prd034_identifier_that_must_remain_complete",
      created_at: "2026-07-13T08:00:00Z",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  request_id: "req_query",
  trace_id: "trc_query",
};

const auditExportHeaders = {
  "Content-Type": "text/csv; charset=utf-8",
  "Content-Disposition": 'attachment; filename="metahub-audit-20260731-a1b2c3d4.csv"',
  ETag: `"${"a".repeat(64)}"`,
  "X-MetaHub-Audit-Export-ID": `aex_${"b".repeat(32)}`,
  "X-MetaHub-Audit-Export-Row-Count": "1",
  "X-Request-ID": "req_audit_export_result",
  "X-Trace-ID": "trc_audit_export_result",
};

/** 构造具备浏览器 Headers/blob/json 最小合同的审计导出响应。 */
function auditExportResponse({ headers = {}, blob = new Blob(["csv"]), ok = true, status = 200, payload = {} } = {}) {
  return {
    ok,
    status,
    headers: new Headers({ ...auditExportHeaders, ...headers }),
    async blob() { return blob; },
    async json() { return payload; },
  };
}

/** 构造包含完整 tenant/user/role/Session/revision 的前端 owner 身份。 */
function auditAdminContext(overrides = {}) {
  // 测试身份显式携带审计读写权限，禁止 helper 根据 admin 角色名隐式补权。
  return withPermissionSnapshot(
    {
      tenant: { tenant_id: "tenant_a" },
      user: { tenant_id: "tenant_a", user_id: "user_a", email: "admin@example.com", role: "admin" },
      session: { session_id: "session_a", session_revision: 7 },
      ...overrides,
    },
    [PERMISSIONS.AUDIT_EXPORT, PERMISSIONS.AUDIT_READ],
  );
}

test("audit logs client maps all filters without accepting tenant id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map(),
      async json() {
        return auditPayload;
      },
    };
  };

  const result = await listAuditLogs({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    actorUserId: "user_admin",
    action: "provider_key.updated",
    objectType: "provider_key",
    objectId: "provider_key_1",
    requestId: "req_1",
    createdFrom: "2026-07-13T00:00:00.000Z",
    createdTo: "2026-07-14T00:00:00.000Z",
    limit: 20,
    offset: 40,
    tenantId: "tenant_must_not_be_sent",
  });

  const requestUrl = new URL(calls[0].url);
  assert.equal(result.state, "ok");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(requestUrl.pathname, "/audit-logs");
  assert.equal(requestUrl.searchParams.get("actor_user_id"), "user_admin");
  assert.equal(requestUrl.searchParams.get("action"), "provider_key.updated");
  assert.equal(requestUrl.searchParams.get("object_type"), "provider_key");
  assert.equal(requestUrl.searchParams.get("object_id"), "provider_key_1");
  assert.equal(requestUrl.searchParams.get("request_id"), "req_1");
  assert.equal(requestUrl.searchParams.get("created_from"), "2026-07-13T00:00:00.000Z");
  assert.equal(requestUrl.searchParams.get("created_to"), "2026-07-14T00:00:00.000Z");
  assert.equal(requestUrl.searchParams.get("offset"), "40");
  assert.equal(requestUrl.searchParams.has("tenant_id"), false);
  assert.doesNotMatch(calls[0].url, /tenant_must_not_be_sent/);
});

test("audit logs client preserves unified error code and request id", async () => {
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
    () => listAuditLogs({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_denied");
      assert.equal(error.traceId, "trc_denied");
      return true;
    },
  );
});

test("audit export maps current filters without tenant or pagination and validates frozen CSV metadata", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return auditExportResponse();
  };

  const result = await exportAuditLogs({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    actorUserId: "user_admin",
    action: "policy.updated",
    objectType: "policy",
    objectId: "pol_1",
    requestId: "req_filtered_operation",
    createdFrom: "2026-07-30T00:00:00.000Z",
    createdTo: "2026-07-31T00:00:00.000Z",
    operationRequestId: "req_manual_export_1",
    tenantId: "tenant_must_not_be_sent",
    limit: 1,
    offset: 9,
    signal: new AbortController().signal,
  });

  const requestUrl = new URL(calls[0].url);
  assert.equal(requestUrl.pathname, "/audit-logs/export");
  assert.equal(requestUrl.searchParams.get("actor_user_id"), "user_admin");
  assert.equal(requestUrl.searchParams.get("action"), "policy.updated");
  assert.equal(requestUrl.searchParams.get("object_type"), "policy");
  assert.equal(requestUrl.searchParams.get("object_id"), "pol_1");
  assert.equal(requestUrl.searchParams.get("request_id"), "req_filtered_operation");
  assert.equal(requestUrl.searchParams.get("created_from"), "2026-07-30T00:00:00.000Z");
  assert.equal(requestUrl.searchParams.get("created_to"), "2026-07-31T00:00:00.000Z");
  assert.equal(requestUrl.searchParams.has("tenant_id"), false);
  assert.equal(requestUrl.searchParams.has("limit"), false);
  assert.equal(requestUrl.searchParams.has("offset"), false);
  assert.equal(calls[0].options.headers.Accept, "text/csv");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_manual_export_1");
  assert.equal(result.filename, "metahub-audit-20260731-a1b2c3d4.csv");
  assert.equal(result.auditExportId, `aex_${"b".repeat(32)}`);
  assert.equal(result.contentSha256, "a".repeat(64));
  assert.equal(result.rowCount, 1);
  assert.equal(result.blob.size, 3);
  assert.equal(result.requestId, "req_audit_export_result");
  assert.equal(result.traceId, "trc_audit_export_result");
});

test("audit export rejects malformed headers and empty Blob without reflecting header values", async () => {
  const cases = [
    { name: "content type", headers: { "Content-Type": "text/html" } },
    { name: "path filename", headers: { "Content-Disposition": 'attachment; filename="../tenant.csv"' } },
    { name: "non ASCII filename", headers: { "Content-Disposition": 'attachment; filename="méta.csv"' } },
    { name: "export id", headers: { "X-MetaHub-Audit-Export-ID": "aex_bad" } },
    { name: "weak etag", headers: { ETag: `W/"${"a".repeat(64)}"` } },
    { name: "uppercase etag", headers: { ETag: `"${"A".repeat(64)}"` } },
    { name: "negative rows", headers: { "X-MetaHub-Audit-Export-Row-Count": "-1" } },
    { name: "too many rows", headers: { "X-MetaHub-Audit-Export-Row-Count": "10001" } },
    { name: "noncanonical rows", headers: { "X-MetaHub-Audit-Export-Row-Count": "01" } },
    { name: "missing request", headers: { "X-Request-ID": "<script>request</script>" } },
    { name: "missing trace", headers: { "X-Trace-ID": "<script>trace</script>" } },
    { name: "empty blob", blob: new Blob([]) },
  ];

  for (const scenario of cases) {
    globalThis.fetch = async () => auditExportResponse(scenario);
    await assert.rejects(
      () => exportAuditLogs({
        controlApiUrl: "http://control.local",
        token: "jwt-token",
        operationRequestId: "req_malformed_export",
      }),
      (error) => {
        assert.equal(error instanceof AuthApiError, true, scenario.name);
        assert.equal(error.code, "audit_export.unavailable", scenario.name);
        assert.equal(
          error.requestId,
          scenario.name === "missing request" ? "req_malformed_export" : "req_audit_export_result",
          scenario.name,
        );
        assert.doesNotMatch(error.message, /tenant|script|\.\./i, scenario.name);
        return true;
      },
    );
  }
});

test("audit export preserves only unified error code and safe request id", async () => {
  globalThis.fetch = async () => auditExportResponse({
    ok: false,
    status: 403,
    payload: {
      code: "auth.permission_denied",
      message: "raw backend detail must not be rendered",
      request_id: "req_export_denied",
      trace_id: "trc_export_denied",
    },
  });
  await assert.rejects(
    () => exportAuditLogs({
      controlApiUrl: "http://control.local",
      token: "jwt-token",
      operationRequestId: "req_export_attempt",
    }),
    (error) => {
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_export_denied");
      assert.equal(error.traceId, "trc_export_denied");
      assert.equal(error.message, "审计日志导出失败");
      assert.doesNotMatch(error.message, /raw backend/);
      return true;
    },
  );

  globalThis.fetch = async () => auditExportResponse({
    ok: false,
    status: 502,
    headers: { "X-Request-ID": "<img src=x>", "X-Trace-ID": "bad trace" },
    payload: { code: "<script>", request_id: "../../secret", trace_id: "bad trace" },
  });
  await assert.rejects(
    () => exportAuditLogs({
      controlApiUrl: "http://control.local",
      token: "jwt-token",
      operationRequestId: "req_safe_fallback",
    }),
    (error) => {
      assert.equal(error.code, "audit_export.unavailable");
      assert.equal(error.requestId, "req_safe_fallback");
      assert.equal(error.traceId, "-");
      return true;
    },
  );
});

test("audit CSV download uses one object URL and revokes it on success or click failure", () => {
  const result = {
    blob: new Blob(["csv"]),
    filename: "metahub-audit-20260731-a1b2c3d4.csv",
    auditExportId: `aex_${"b".repeat(32)}`,
    contentSha256: "a".repeat(64),
    rowCount: 1,
    requestId: "req_download",
    traceId: "trc_download",
  };
  const events = [];
  const link = {
    click() { events.push("click"); },
    remove() { events.push("remove"); },
  };
  triggerAuditCsvDownload(result, {
    URL: {
      createObjectURL(blob) { events.push(`create:${blob.size}`); return "blob:audit"; },
      revokeObjectURL(url) { events.push(`revoke:${url}`); },
    },
    document: {
      createElement(tag) { assert.equal(tag, "a"); return link; },
      body: { append(node) { assert.equal(node, link); events.push("append"); } },
    },
  });
  assert.deepEqual(events, ["create:3", "append", "click", "remove", "revoke:blob:audit"]);
  assert.equal(link.download, result.filename);

  const failureEvents = [];
  assert.throws(() => triggerAuditCsvDownload(result, {
    URL: {
      createObjectURL() { return "blob:audit-failure"; },
      revokeObjectURL(url) { failureEvents.push(`revoke:${url}`); },
    },
    document: {
      createElement() {
        return { click() { throw new Error("click failed"); }, remove() { failureEvents.push("remove"); } };
      },
      body: { append() {} },
    },
  }), /click failed/);
  assert.deepEqual(failureEvents, ["remove", "revoke:blob:audit-failure"]);
});

test("audit export lifecycle binds full identity navigation generation and abort", () => {
  const context = auditAdminContext();
  const identity = auditExportIdentitySnapshot(context);
  assert.match(identity, /tenant_a/);
  assert.notEqual(identity, auditExportIdentitySnapshot({
    ...context,
    session: { ...context.session, session_revision: 8 },
  }));
  assert.equal(auditExportIdentitySnapshot({
    ...withPermissionSnapshot(
      { ...context, user: { ...context.user, role: "developer" } },
      [PERMISSIONS.AUDIT_READ],
    ),
  }), "");

  const lifecycle = createAuditExportLifecycle();
  const first = lifecycle.begin({ identity, navigation: "audit-logs" });
  assert.ok(first);
  assert.equal(lifecycle.isBusy(), true);
  assert.equal(lifecycle.begin({ identity, navigation: "audit-logs" }), null);
  assert.equal(lifecycle.isCurrent(first, { identity, navigation: "audit-logs" }), true);
  assert.equal(lifecycle.isCurrent(first, { identity, navigation: "overview" }), false);
  assert.equal(lifecycle.isCurrent(first, {
    identity: auditExportIdentitySnapshot({ ...context, tenant: { tenant_id: "tenant_b" }, user: { ...context.user, tenant_id: "tenant_b" } }),
    navigation: "audit-logs",
  }), false);
  lifecycle.clear();
  assert.equal(first.signal.aborted, true);
  assert.equal(lifecycle.isCurrent(first, { identity, navigation: "audit-logs" }), false);
  assert.equal(lifecycle.isBusy(), false);
  const second = lifecycle.begin({ identity, navigation: "audit-logs" });
  assert.ok(second.generation > first.generation);
  lifecycle.finish(second);
  assert.equal(lifecycle.isBusy(), false);
});

test("each new audit export request id is safe and unique", () => {
  const first = createAuditExportRequestId();
  const second = createAuditExportRequestId();
  assert.match(first, /^console_audit_export_[A-Za-z0-9_]+$/);
  assert.match(second, /^console_audit_export_[A-Za-z0-9_]+$/);
  assert.notEqual(first, second);
  assert.ok(first.length <= 128);
  assert.ok(second.length <= 128);
});

test("audit panel renders safe expandable summaries, full ids and mobile labels", () => {
  const html = renderAuditLogPanel(
    {
      state: "ok",
      ...auditPayload,
      filters: { action: "provider_key.updated", objectType: "provider_key" },
      exportState: { status: "idle" },
    },
    auditAdminContext(),
  );

  assert.match(html, /audit-log-filter-form/);
  assert.match(html, /<details data-label="摘要"/);
  assert.match(html, /展开摘要/);
  assert.match(html, /变更前/);
  assert.match(html, /变更后/);
  assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>unsafe<\/script>/);
  assert.match(html, new RegExp(longRequestId));
  assert.match(html, /data-copy-audit-id=/);
  assert.match(html, /id="audit-log-export-csv"/);
  assert.match(html, /aria-label="导出当前筛选的 CSV"/);
  assert.match(html, /data-audit-export-feedback[^>]*hidden/);
  assert.match(html, /audit\.exported/);
  assert.match(html, /policy_snapshot\.published/);
  assert.match(html, /provider_error_map/);
  for (const label of ["时间", "操作者", "动作", "对象", "变更字段", "来源 IP", "request_id", "摘要"]) {
    assert.match(html, new RegExp(`data-label="${label}"`));
  }
  assert.doesNotMatch(html, /\.\.\./);
});

test("audit panel renders bounded export success and error feedback without raw fields", () => {
  const completed = renderAuditLogPanel({
    state: "ok",
    ...auditPayload,
    exportState: {
      status: "completed",
      auditExportId: `aex_${"b".repeat(32)}`,
      contentSha256: "a".repeat(64),
      rowCount: 1,
      requestId: "req_export_result",
      filename: "must-not-render.csv",
      traceId: "trc_must_not_render",
      rawCsv: "secret,csv",
    },
  }, auditAdminContext());
  assert.match(completed, new RegExp(`aex_${"b".repeat(32)}`));
  assert.match(completed, new RegExp("a".repeat(64)));
  assert.match(completed, /行数:/);
  assert.match(completed, /req_export_result/);
  assert.doesNotMatch(completed, /must-not-render/);
  assert.doesNotMatch(completed, /secret,csv/);

  const failed = renderAuditLogPanel({
    state: "ok",
    ...auditPayload,
    exportState: {
      status: "error",
      code: "audit_export.too_large",
      requestId: "req_too_large",
      message: "raw SQL and header dump must not render",
    },
  }, auditAdminContext());
  assert.match(failed, /audit_export\.too_large/);
  assert.match(failed, /req_too_large/);
  assert.doesNotMatch(failed, /raw SQL/);
  assert.doesNotMatch(failed, /header dump/);
});

test("audit panel shows code/request_id errors and stable empty pagination", () => {
  const errorHtml = renderAuditLogPanel(
    { state: "error", message: "查询失败", code: "audit_log.filter_invalid", requestId: "req_bad_filter" },
    auditAdminContext(),
  );
  const emptyHtml = renderAuditLogPanel(
    { state: "ok", audit_logs: [], total: 0, limit: 20, offset: 0 },
    auditAdminContext(),
  );
  const overflowHtml = renderAuditLogPanel(
    { state: "ok", audit_logs: [], total: 10, limit: 20, offset: 40 },
    auditAdminContext(),
  );

  assert.match(errorHtml, /audit_log.filter_invalid/);
  assert.match(errorHtml, /req_bad_filter/);
  assert.match(errorHtml, /audit-log-filter-form/);
  assert.match(errorHtml, /audit-log-filter-reset/);
  assert.match(emptyHtml, /0-0 \/ 0/);
  assert.match(emptyHtml, /id="audit-log-prev"[^>]*disabled/);
  assert.match(emptyHtml, /id="audit-log-next"[^>]*disabled/);
  assert.match(overflowHtml, /0-0 \/ 10/);
  assert.doesNotMatch(overflowHtml, /41-10/);
});

test("audit navigation renders for admin and remains hidden from developer", () => {
  const navigation = {
    navigation: {
      active_navigation_item_id: "audit-logs",
      navigation_items: [
        {
          id: "audit-logs",
          label: "审计日志",
          group: "安全与治理",
          target_section: "audit-logs",
          description: "管理审计",
          empty_state_hint: "暂无审计",
          order: 90,
        },
      ],
    },
    activeNavigationItemId: "audit-logs",
  };
  const adminContainer = { innerHTML: "" };
  const developerContainer = { innerHTML: "" };

  renderDeveloperStart(adminContainer, {
    authContext: withPermissionSnapshot(
      { user: { role: "admin", display_name: "Admin", email: "admin@example.com" }, tenant: { name: "Tenant" } },
      [PERMISSIONS.AUDIT_EXPORT, PERMISSIONS.AUDIT_READ],
    ),
    consoleShellState: navigation,
    auditLogsState: { state: "ok", ...auditPayload },
  });
  renderDeveloperStart(developerContainer, {
    authContext: withPermissionSnapshot(
      { user: { role: "developer", display_name: "Dev", email: "dev@example.com" }, tenant: { name: "Tenant" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
    consoleShellState: navigation,
    auditLogsState: { state: "ok", ...auditPayload },
  });

  assert.match(adminContainer.innerHTML, /audit-logs-panel/);
  assert.match(adminContainer.innerHTML, new RegExp(longRequestId));
  assert.doesNotMatch(developerContainer.innerHTML, /audit-logs-panel/);
  assert.doesNotMatch(developerContainer.innerHTML, new RegExp(longRequestId));
  assert.doesNotMatch(developerContainer.innerHTML, /audit-log-export-csv/);

  assert.equal(renderAuditLogPanel(
    { state: "ok", ...auditPayload, exportState: { status: "completed", auditExportId: "aex_hidden" } },
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  ), "");
});

test("main loader requires audit read permission and current audit navigation", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");

  // 静态门禁检查防止后续重构退化为只看 hash 或角色名，从而在其他页面后台预取审计摘要。
  assert.match(source, /!hasPermission\(authSession\.context, PERMISSIONS\.AUDIT_READ\)/);
  assert.match(source, /consoleShellState\?\.activeNavigationItemId !== "audit-logs"/);
  assert.match(source, /loadAuditLogsState\(consoleShellState, refreshId\)/);
  assert.match(source, /refreshId !== refreshSequence/);
  assert.match(source, /hashNavigation && hashNavigation !== "audit-logs"/);
});

test("main binds export only to an explicit authorized click and clears every owner boundary", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const clickStart = source.indexOf('auditLogExportButton?.addEventListener("click"');
  const clickEnd = source.indexOf('for (const copyButton of document.querySelectorAll("[data-copy-audit-id]"))', clickStart);
  assert.ok(clickStart >= 0 && clickEnd > clickStart);
  const clickHandler = source.slice(clickStart, clickEnd);
  assert.match(clickHandler, /!hasPermission\(authSession\.context, PERMISSIONS\.AUDIT_EXPORT\)/);
  assert.match(clickHandler, /activeNavigationItemId !== "audit-logs"/);
  assert.match(clickHandler, /createAuditExportRequestId\(\)/);
  assert.match(clickHandler, /exportAuditLogs\(\{/);
  assert.match(clickHandler, /triggerAuditCsvDownload\(result\)/);
  assert.match(clickHandler, /await refresh\(\)/);
  assert.doesNotMatch(clickHandler, /tenantId:/);
  assert.doesNotMatch(clickHandler, /limit:/);
  assert.doesNotMatch(clickHandler, /offset:/);

  const timerStart = source.indexOf("window.setInterval(() => {");
  const timerEnd = source.indexOf("}, 15000);", timerStart);
  assert.ok(timerStart >= 0 && timerEnd > timerStart);
  assert.doesNotMatch(source.slice(timerStart, timerEnd), /exportAuditLogs|audit-logs\/export/);
  assert.match(source, /pagehide[\s\S]*?clearAuditExportSessionState\(\)/);
  assert.match(source, /logoutButton\?\.addEventListener\("click"[\s\S]*?clearAuditExportSessionState\(\)/);
  assert.match(source, /itemId !== "audit-logs"[\s\S]*?clearAuditExportSessionState\(\)/);
  assert.match(source, /status === 401[\s\S]*?clearAuditExportSessionState\(\)/);
  assert.match(source, /clearAuditExportStateOnIdentityChange\(authSession\.context, context\)/);
  assert.match(source, /auditExportLifecycle\.clear\(\)/);
});

test("responsive audit CSS hides the header and keeps native details full width", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");

  assert.match(css, /\.audit-log-summary \{\s*display: block;/);
  assert.match(css, /\.audit-export-button \{[\s\S]*?width: 132px;[\s\S]*?height: 36px;/);
  assert.match(css, /\.audit-export-feedback \{[\s\S]*?overflow-wrap: anywhere;[\s\S]*?word-break: break-word;[\s\S]*?white-space: normal;/);
  assert.match(css, /\.audit-export-feedback code \{[\s\S]*?overflow-wrap: anywhere;/);
  assert.match(css, /@media \(max-width: 1100px\)[\s\S]*?\.audit-log-head \{\s*display: none;/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.audit-log-commands \{[\s\S]*?width: 100%;/);
  // 移动端列必须显式清除自动最小宽度，避免 64 位 SHA-256 把 390px 根节点撑宽。
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.audit-export-feedback \{[\s\S]*?grid-template-columns: minmax\(0, 1fr\);/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.audit-export-feedback span,[\s\S]*?min-width: 0;[\s\S]*?word-break: break-word;/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.audit-log-filter-form,[^{]*\{\s*grid-template-columns: 1fr;/);
});
