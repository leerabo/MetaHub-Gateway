/**
 * 测试职责：验证 Provider 错误规则页面的租户 owner、后端权限交互、冲突恢复与日志事实渲染。
 * 安全边界：测试哨兵确保 Token、原始上游正文和跨租户标识不会进入 URL、DOM 或请求载荷。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createErrorMap,
  listErrorMaps,
  updateErrorMap,
} from "../../src/lib/errorMaps.js";
import {
  createErrorMapHandoff,
  createErrorMapPageMemory,
  errorMapIdentitySnapshot,
} from "../../src/features/error-maps/errorMapPageState.js";
import {
  errorMapDraftFromFormData,
  parseHttpStatusesInput,
  renderErrorMapPanel,
} from "../../src/features/error-maps/renderErrorMapPanel.js";
import { renderRequestLogPanel } from "../../src/features/request-logs/renderRequestLogPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const auth = withPermissionSnapshot({
  tenant: { tenant_id: "tenant_056" },
  user: { tenant_id: "tenant_056", user_id: "user_056", role: "admin" },
  session: { session_id: "session_056", session_revision: 8 },
}, [
  PERMISSIONS.PROVIDER_ERROR_MAP_READ,
  PERMISSIONS.PROVIDER_ERROR_MAP_WRITE,
  PERMISSIONS.REQUEST_LOG_READ,
]);

const tenantRuleV1 = {
  error_map_id: "pem_11111111111111111111111111111111",
  source: "tenant",
  readonly: false,
  provider_id: "prv_1",
  provider_code: "deepseek",
  provider_name: "DeepSeek 官方",
  priority: 200,
  http_statuses: [400, 404],
  http_status_class: null,
  error_field: "error.code",
  field_operator: "equals",
  field_pattern: "model_not_found",
  message_operator: null,
  message_pattern: null,
  output_error_code: "gateway.upstream_model_not_found",
  retryable: false,
  user_action: "检查模型映射",
  status: "active",
  version: 1,
  pattern_hash: "a".repeat(64),
};

const listPayload = {
  error_maps: [
    tenantRuleV1,
    {
      error_map_id: "builtin_http_429",
      source: "builtin",
      readonly: true,
      ordinal: 40,
      http_statuses: [429],
      output_error_code: "gateway.upstream_rate_limited",
      retryable: true,
      status: "active",
      version: 1,
    },
  ],
  provider_options: [{ provider_id: "prv_1", provider_code: "deepseek", name: "DeepSeek 官方", status: "active", base_url: "MUST_NOT_COPY" }],
  head_revision: 1,
  content_hash: "b".repeat(64),
  builtin_catalog_version: "provider-error-builtin-v1",
  updated_at: "2026-07-29T08:00:00Z",
  total: 2,
  limit: 100,
  offset: 0,
};

/** 构造绑定完整认证身份的页面 owner，供迟到响应和离页竞态断言复用。 */
function owner(identity = errorMapIdentitySnapshot(auth)) {
  return { identity, navigation: "error-maps" };
}

test("error-map handoff is one-shot, owner-bound and rejects incomplete rule identity", () => {
  const identity = errorMapIdentitySnapshot(auth);
  const handoff = createErrorMapHandoff();
  assert.equal(handoff.stage({
    identity,
    tenantId: "tenant_056",
    source: "tenant",
    errorMapId: tenantRuleV1.error_map_id,
    matchedRuleVersion: "not-a-version",
  }), false);
  assert.deepEqual(handoff.snapshot(), { generation: 1, pending: false });

  assert.equal(handoff.stage({
    identity,
    tenantId: "tenant_056",
    source: "tenant",
    errorMapId: tenantRuleV1.error_map_id,
    matchedRuleVersion: "2",
  }), true);
  // tenant owner 不匹配时同样消费即销毁，随后正确租户也不能重放旧项。
  assert.equal(handoff.consume({ identity, tenantId: "tenant_other", navigation: "error-maps" }), null);
  assert.equal(handoff.consume({ identity, tenantId: "tenant_056", navigation: "error-maps" }), null);

  assert.equal(handoff.stage({
    identity,
    tenantId: "tenant_056",
    source: "tenant",
    errorMapId: tenantRuleV1.error_map_id,
    matchedRuleVersion: "2",
  }), true);
  // 错误导航会消费并销毁，而不是给后续正确导航留下可重放引用。
  assert.equal(handoff.consume({ identity, tenantId: "tenant_056", navigation: "request-logs" }), null);
  assert.equal(handoff.snapshot().pending, false);

  assert.equal(handoff.stage({
    identity,
    tenantId: "tenant_056",
    source: "builtin",
    errorMapId: "builtin_http_429",
    builtinCatalogVersion: "provider-error-builtin-v1",
  }), true);
  assert.deepEqual(handoff.consume({ identity, tenantId: "tenant_056", navigation: "error-maps" }), {
    source: "builtin",
    errorMapId: "builtin_http_429",
    requestedVersion: null,
    requestedCatalogVersion: "provider-error-builtin-v1",
  });
  assert.equal(handoff.consume({ identity, tenantId: "tenant_056", navigation: "error-maps" }), null);
});

test("PAGE-081 keeps frozen tenant version separate from the current exact resource", () => {
  const memory = createErrorMapPageMemory({ requestIdFactory: () => "req" });
  const identity = errorMapIdentitySnapshot(auth);
  const ticket = memory.beginRefresh({
    identity,
    navigation: "error-maps",
    query: "tenant-reference",
    reference: {
      source: "tenant",
      errorMapId: tenantRuleV1.error_map_id,
      requestedVersion: 1,
      requestedCatalogVersion: null,
    },
  });
  assert.equal(memory.acceptRefresh(ticket, {
    ...listPayload,
    error_maps: [{ ...tenantRuleV1, version: 3 }],
    total: 1,
  }, owner(identity)), true);
  const html = renderErrorMapPanel(memory.snapshot(), auth, { source: "tenant" });
  assert.match(html, /请求时版本 v1 \/ 当前版本 v3/);
  assert.match(html, /data-error-map-reference="true"/);
  assert.match(html, /历史结果以日志中冻结的 ProviderErrorFact 为准/);
  assert.doesNotMatch(html, /request_id|tenant_056/);
  memory.leave();
  assert.equal(memory.snapshot().reference, null);
});

test("PAGE-081 edit dialog exposes only its escaped frozen CAS version", () => {
  const memory = createErrorMapPageMemory({ requestIdFactory: () => "req" });
  const identity = errorMapIdentitySnapshot(auth);
  const ticket = memory.beginRefresh({ identity, navigation: "error-maps", query: "all" });
  assert.equal(memory.acceptRefresh(ticket, listPayload, owner(identity)), true);
  assert.equal(memory.openEdit(tenantRuleV1.error_map_id), true);
  let html = renderErrorMapPanel(memory.snapshot(), auth, { source: "tenant" });
  assert.match(html, /class="error-map-dialog"[^>]*data-error-map-version="1"/);
  assert.doesNotMatch(html, /data-error-map-version="[^"]*"[^>]*tenant_056/);
  const editState = memory.snapshot();
  const escapedHtml = renderErrorMapPanel({
    ...editState,
    dialog: { ...editState.dialog, baseVersion: '1" data-tenant-id="forged' },
  }, auth, { source: "tenant" });
  assert.match(escapedHtml, /data-error-map-version="1&quot; data-tenant-id=&quot;forged"/);
  assert.doesNotMatch(escapedHtml, /data-tenant-id="forged"/);

  assert.equal(memory.closeDialog(), true);
  assert.equal(memory.openCreate(), true);
  html = renderErrorMapPanel(memory.snapshot(), auth, { source: "tenant" });
  assert.doesNotMatch(html, /data-error-map-version=/);
});

test("PAGE-081 locates builtin reference without claiming the current catalog produced history", () => {
  const memory = createErrorMapPageMemory({ requestIdFactory: () => "req" });
  const identity = errorMapIdentitySnapshot(auth);
  const ticket = memory.beginRefresh({
    identity,
    navigation: "error-maps",
    query: "builtin-reference",
    reference: {
      source: "builtin",
      errorMapId: "builtin_http_429",
      requestedVersion: null,
      requestedCatalogVersion: "provider-error-builtin-v0",
    },
  });
  assert.equal(memory.acceptRefresh(ticket, listPayload, owner(identity)), true);
  const html = renderErrorMapPanel(memory.snapshot(), auth, { source: "builtin" });
  assert.match(html, /请求时目录 provider-error-builtin-v0 \/ 当前目录 provider-error-builtin-v1/);
  assert.match(html, /已定位平台只读条目/);
  assert.match(html, /data-error-map-reference="true"/);
});

test("error-map client keeps exact conflict GET mutually exclusive and strips owner fields", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: options.method === "POST" ? 201 : 200,
      headers: { get() { return null; } },
      async json() { return { error_map: { ...tenantRuleV1, version: options.method === "PATCH" ? 2 : 1 }, changed: true }; },
    };
  };
  await listErrorMaps({
    controlApiUrl: "http://control.local",
    token: "jwt",
    errorMapId: tenantRuleV1.error_map_id,
    providerId: "SHOULD_NOT_SEND",
    source: "tenant",
    limit: 9,
    offset: 90,
    requestId: "req_conflict_get",
  });
  const exactUrl = new URL(calls[0].url);
  assert.equal(exactUrl.searchParams.get("error_map_id"), tenantRuleV1.error_map_id);
  for (const forbidden of ["provider_id", "source", "status", "output_error_code", "limit", "offset"]) {
    assert.equal(exactUrl.searchParams.has(forbidden), false);
  }
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_conflict_get");

  const unsafeDraft = { ...tenantRuleV1, tenant_id: "tenant_evil", actor_id: "user_evil", pattern_hash: "secret", version: 91 };
  await createErrorMap({ controlApiUrl: "http://control.local", token: "jwt", draft: unsafeDraft, requestId: "req_post" });
  await updateErrorMap({
    controlApiUrl: "http://control.local", token: "jwt", errorMapId: tenantRuleV1.error_map_id,
    expectedVersion: 1, draft: unsafeDraft, requestId: "req_patch",
  });
  const postBody = JSON.parse(calls[1].options.body);
  const patchBody = JSON.parse(calls[2].options.body);
  assert.equal(postBody.tenant_id, undefined);
  assert.equal(postBody.actor_id, undefined);
  assert.equal(postBody.pattern_hash, undefined);
  assert.equal(postBody.version, undefined);
  assert.equal(patchBody.expected_version, 1);
  assert.equal(patchBody.error_map_id, undefined);
  assert.doesNotMatch(calls[2].url, /tenant_evil|user_evil|secret/);
});

test("error-map client converts every parsed HTTP error to AuthApiError with request_id", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: { get(name) { return name === "X-Request-ID" ? "req_header" : null; } },
    async json() {
      // 模拟后端统一权限错误，客户端必须优先采用响应体中的 request_id 与 trace_id。
      return { code: "auth.permission_denied", message: "Permission denied.", request_id: "req_body", trace_id: "trc_body" };
    },
  });
  await assert.rejects(
    () => listErrorMaps({ controlApiUrl: "http://control.local", token: "jwt" }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 403);
      assert.equal(error.requestId, "req_body");
      return true;
    },
  );
});

test("PAGE-081 state keeps draft through 409 and uses three different request IDs", () => {
  const requestIds = ["req_patch_v1", "req_conflict_get", "req_patch_v2"];
  const memory = createErrorMapPageMemory({ requestIdFactory: () => requestIds.shift() });
  const identity = errorMapIdentitySnapshot(auth);
  const listTicket = memory.beginRefresh({ identity, navigation: "error-maps", query: "all" });
  assert.equal(memory.acceptRefresh(listTicket, listPayload, owner(identity)), true);
  assert.equal(memory.openEdit(tenantRuleV1.error_map_id), true);
  assert.equal(memory.updateDraft({ ...tenantRuleV1, priority: 201 }), true);
  const patchV1 = memory.acquireMutation(owner(identity)).ticket;
  assert.equal(patchV1.requestId, "req_patch_v1");
  assert.equal(patchV1.expectedVersion, 1);
  assert.equal(memory.acceptMutationFailure(patchV1, {
    status: 409, code: "error_map.version_conflict", message: "版本冲突", requestId: "req_patch_v1",
  }, owner(identity)), true);
  assert.equal(memory.snapshot().dialog.draft.priority, 201);
  assert.equal(memory.snapshot().dialog.conflicted, true);

  const conflict = memory.beginConflictRefresh(owner(identity));
  assert.equal(conflict.requestId, "req_conflict_get");
  const v2 = { ...tenantRuleV1, priority: 205, version: 2 };
  assert.equal(memory.acceptConflictRefresh(conflict, { error_maps: [v2] }, owner(identity)), true);
  assert.equal(memory.snapshot().dialog.draft.priority, 201);
  assert.equal(memory.snapshot().dialog.sideLatest.version, 2);
  assert.equal(memory.adoptConflict(), true);
  assert.equal(memory.snapshot().dialog.baseVersion, 2);
  assert.equal(memory.updateDraft({ ...v2, priority: 206 }), true);
  const patchV2 = memory.acquireMutation(owner(identity)).ticket;
  assert.equal(patchV2.requestId, "req_patch_v2");
  assert.equal(patchV2.expectedVersion, 2);
  assert.notEqual(patchV2.requestId, patchV1.requestId);
  assert.notEqual(patchV2.requestId, conflict.requestId);
  assert.equal(memory.acceptMutationSuccess(patchV2, {
    changed: true, error_map: { ...v2, priority: 206, version: 3 }, head_revision: 2,
  }, owner(identity)), true);
  assert.equal(memory.snapshot().dialog, null);
});

test("PAGE-081 state only reuses mutation ID when network result is unknown", () => {
  const ids = ["req_network_unknown", "req_after_http"];
  const memory = createErrorMapPageMemory({ requestIdFactory: () => ids.shift() });
  const identity = errorMapIdentitySnapshot(auth);
  const listTicket = memory.beginRefresh({ identity, navigation: "error-maps", query: "all" });
  memory.acceptRefresh(listTicket, listPayload, owner(identity));
  memory.openEdit(tenantRuleV1.error_map_id);
  memory.updateDraft({ ...tenantRuleV1, priority: 220 });
  const first = memory.acquireMutation(owner(identity)).ticket;
  memory.acceptMutationFailure(first, new TypeError("network failed"), owner(identity));
  const retry = memory.acquireMutation(owner(identity)).ticket;
  assert.equal(retry.requestId, "req_network_unknown");
  memory.acceptMutationFailure(retry, { status: 422, code: "error_map.input_invalid", message: "invalid" }, owner(identity));
  const afterHttp = memory.acquireMutation(owner(identity)).ticket;
  assert.equal(afterHttp.requestId, "req_after_http");
});

test("PAGE-081 renderer is role gated, escapes patterns and keeps developer read-only", () => {
  const memory = createErrorMapPageMemory({ requestIdFactory: () => "req" });
  const identity = errorMapIdentitySnapshot(auth);
  const ticket = memory.beginRefresh({ identity, navigation: "error-maps", query: "all" });
  memory.acceptRefresh(ticket, {
    ...listPayload,
    error_maps: [{ ...tenantRuleV1, field_pattern: '<script>RAW_PATTERN</script>', provider_name: '<img src=x onerror="XSS">' }],
  }, owner(identity));
  const adminHtml = renderErrorMapPanel(memory.snapshot(), auth, {});
  const developerHtml = renderErrorMapPanel(
    memory.snapshot(),
    withPermissionSnapshot(
      { ...auth, user: { ...auth.user, role: "developer" } },
      [PERMISSIONS.PROVIDER_ERROR_MAP_READ],
    ),
    {},
  );
  assert.match(adminHtml, /error-map-create/);
  assert.match(adminHtml, /&lt;script&gt;RAW_PATTERN&lt;\/script&gt;/);
  assert.match(adminHtml, /&lt;img src=x onerror=&quot;XSS&quot;&gt;/);
  assert.doesNotMatch(adminHtml, /<script>|<img/);
  assert.doesNotMatch(developerHtml, /error-map-create|data-error-map-edit|data-error-map-status/);
  assert.equal(renderErrorMapPanel(
    memory.snapshot(),
    withPermissionSnapshot(
      { ...auth, user: { ...auth.user, role: "viewer" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
    {},
  ), "");
});

test("PAGE-081 form parser supports equals/contains only and validates bounded HTTP text", () => {
  const values = new Map([
    ["providerId", "prv_1"], ["priority", "300"], ["httpStatuses", "404, 400,404"],
    ["httpStatusClass", ""], ["errorField", "error.code"], ["fieldOperator", "contains"],
    ["fieldPattern", "model_not_found"], ["messagePattern", ""],
    ["outputErrorCode", "gateway.upstream_model_not_found"], ["status", "active"],
  ]);
  const data = { get(name) { return values.get(name); } };
  assert.deepEqual(errorMapDraftFromFormData(data).http_statuses, [400, 404]);
  assert.deepEqual(parseHttpStatusesInput("404, 400,404"), [400, 404]);
  assert.equal(Number.isNaN(parseHttpStatusesInput("40x")[0]), true);
});

test("PAGE-082 renders attempt and stream facts without raw Provider body", () => {
  const fact = {
    provider_error_fact_id: "pef_0123456789abcdef0123456789abcdef",
    matched_rule_source: "tenant",
    matched_error_map_id: tenantRuleV1.error_map_id,
    matched_rule_version: 2,
    matched_pattern_hash: "e".repeat(64),
    match_basis: "compound",
    error_map_revision: 7,
    error_map_snapshot_source: "fresh",
    normalized_output_error_code: '<script>NORMALIZED</script>',
    owner_terminal_error_code: "gateway.upstream_model_not_found",
    transport_kind: "none",
    upstream_http_status: 404,
    retryable: false,
    provider_error_code_class: "model_not_found_error",
    provider_error_type_class: "[REDACTED]",
    provider_error_code_present: true,
    provider_error_type_present: true,
    message_present: true,
    body_length_bucket: "256_1023",
    is_json: true,
    body_truncated: false,
    projection_hash: "c".repeat(64),
    overlay_content_hash: "d".repeat(64),
    raw_body: "sk-provider-secret RAW_RESPONSE_MUST_NOT_RENDER",
    message: "PROMPT_MUST_NOT_RENDER",
  };
  const log = { tenant_id: "tenant_056", request_id: "req_056", status: "failed", created_at: "2026-07-29T08:00:00Z" };
  const html = renderRequestLogPanel({
    state: "ok", request_logs: [], total: 0, limit: 20, offset: 0, filters: {},
    detail: {
      request_log: {
        log,
        routing_attempts: [{ attempt_sequence: 1, attempt_role: "primary", status: "failed", provider_error_fact: fact }],
        stream_session: {
          stream_session_id: "ss_1", status: "upstream_failed",
          completed_at: "2026-07-29T12:00:00Z",
          upstream_http_status: 404,
          retryable: false,
          provider_error_fact: { ...fact, matched_rule_source: "builtin", matched_error_map_id: "builtin_model_not_found_signal", matched_rule_version: null, builtin_catalog_version: "provider-error-builtin-v1" },
        },
      },
    },
  }, auth);
  assert.match(html, /模型服务结果/);
  assert.match(html, /data-provider-error-fact-owner="attempt"/);
  assert.match(html, /data-provider-error-fact-owner="stream_session"/);
  assert.doesNotMatch(html, /#error-maps|data-provider-error-map|data-provider-error-catalog-jump/);
  assert.equal((html.match(/<dt>处理结果<\/dt>\s*<dd>已统一处理<\/dd>/gu) ?? []).length, 2);
  assert.match(html, /<dt>可重试<\/dt>\s*<dd>否<\/dd>/u);
  assert.match(html, /<dt>连接状态<\/dt>\s*<dd>服务已响应<\/dd>/u);
  assert.doesNotMatch(html, /sk-provider-secret|RAW_RESPONSE_MUST_NOT_RENDER|PROMPT_MUST_NOT_RENDER|<script>/);
  assert.doesNotMatch(html, /pem_1111|pef_012345|builtin_model_not_found_signal|provider-error-builtin-v1|NORMALIZED/);
  assert.match(html, /2026/);
});

test("PRD-147 replaces the retired Error Maps runtime with Workspace Provider Engineering", async () => {
  const [entry, renderer, requestLog] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/request-logs/renderRequestLogPanel.js", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(entry, /errorMapPageMemory|errorMapHandoff|bindErrorMapInteractions|loadErrorMapsState|#error-maps/u);
  assert.doesNotMatch(renderer, /renderErrorMapPanel|error-maps-panel/u);
  assert.match(renderer, /case "error-maps"[\s\S]*renderWorkspaceProviderErrorMaps/u);
  assert.doesNotMatch(requestLog, /#error-maps|data-provider-error-map-jump/u);
  assert.match(entry, /workspaceProviderEngineeringController/u);
  assert.match(entry, /listWorkspaceProviderErrorOverrides/u);
});
