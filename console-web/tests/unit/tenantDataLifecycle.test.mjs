import assert from "node:assert/strict";
import test from "node:test";

import {
  createTenantExport,
  downloadTenantExport,
  getTenantDataManagement,
  requestTenantDeletion,
} from "../../src/lib/tenantDataLifecycle.js";
import { renderTenantDataLifecycle } from "../../src/features/tenant-data-lifecycle/renderTenantDataLifecycle.js";
import {
  createTenantDataLifecyclePageState,
  tenantDataLifecycleOwner,
} from "../../src/features/tenant-data-lifecycle/tenantDataLifecyclePageState.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const JOB_ID = `tex_${"a".repeat(32)}`;
const DELETION_ID = `del_${"b".repeat(32)}`;

function context(permissions = [
  PERMISSIONS.TENANT_DATA_READ,
  PERMISSIONS.TENANT_DATA_EXPORT,
  PERMISSIONS.TENANT_DATA_DOWNLOAD,
  PERMISSIONS.TENANT_DATA_DELETE,
], tenantId = "tenant_page121", userId = "usr_page121") {
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, slug: "page121", name: "Page 121" },
    user: { tenant_id: tenantId, user_id: userId, email: "admin@example.test", role: "admin" },
    session: { session_id: "ses_page121" },
  }, permissions, { permissionRevision: 24, authorizationFingerprint: "page121_authz" });
}

function shell(authContext = context()) {
  return {
    activeNavigationItemId: "tenant-data-lifecycle",
    navigation: {
      active_navigation_item_id: "tenant-data-lifecycle",
      permission_codes: authContext.permission_codes,
      permission_revision: authContext.permission_revision,
      authorization_fingerprint: authContext.authorization_fingerprint,
      navigation_items: [{ id: "tenant-data-lifecycle", target_section: "tenant-data-lifecycle", required_permissions: [PERMISSIONS.TENANT_DATA_READ] }],
    },
  };
}

function exportJob(overrides = {}) {
  return {
    tenant_export_job_id: JOB_ID,
    status: "completed",
    schema_version: "metahub.tenant-export/v1",
    snapshot_at: "2026-08-18T00:00:00Z",
    usage_snapshot_sequence: 0,
    audit_snapshot_sequence: 0,
    manifest: { files: ["tenant.json"] },
    content_size: 128,
    content_sha256: "c".repeat(64),
    expires_at: "2026-08-19T00:00:00Z",
    download_count: 0,
    error_code: null,
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:01Z",
    completed_at: "2026-08-18T00:00:01Z",
    ...overrides,
  };
}

function headers(values = {}) {
  return new Headers(values);
}

test("tenant data client never sends a tenant selector and preserves exact destructive body", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith("/data-management")) return new Response(JSON.stringify({ state: { lifecycle_status: "active", revision: 7, cache_generation: 2, updated_at: "2026-08-18T00:00:00Z" }, exports: [exportJob()], deletion_request: null, request_id: "req_page121_get", trace_id: "trc_page121_get" }), { status: 200, headers: { "Content-Type": "application/json" } });
    if (String(url).endsWith("/export")) return new Response(JSON.stringify({ export_job: exportJob({ status: "pending", content_size: null, content_sha256: null, completed_at: null }), status_url: `/tenant/exports/${JOB_ID}`, download_url: `/tenant/exports/${JOB_ID}/download`, request_id: "req_page121_export", trace_id: "trc_page121_export" }), { status: 202, headers: { "Content-Type": "application/json" } });
    return new Response(JSON.stringify({ deletion_request: { deletion_request_id: DELETION_ID, status: "requested", tenant_revision: 7, archived_rows: 0, deleted_rows: 0, retained_records: 0, schema_manifest_sha256: "d".repeat(64), cache_invalidated_at: null, completed_at: null, error_code: null, created_at: "2026-08-18T00:00:00Z", updated_at: "2026-08-18T00:00:00Z" }, request_id: "req_page121_delete", trace_id: "trc_page121_delete" }), { status: 202, headers: { "Content-Type": "application/json" } });
  };

  const current = await getTenantDataManagement({ controlApiUrl: "http://control.local", token: "session-token" });
  await createTenantExport({ controlApiUrl: "http://control.local", token: "session-token", idempotencyKey: "idem-export" });
  await requestTenantDeletion({ controlApiUrl: "http://control.local", token: "session-token", idempotencyKey: "idem-delete", confirmation: "page121", tenantRevision: 7 });

  assert.equal(current.exports[0].tenant_export_job_id, JOB_ID);
  assert.equal(calls.every((call) => !call.url.includes("tenant_id=")), true);
  assert.deepEqual(JSON.parse(calls[1].options.body), {});
  assert.deepEqual(JSON.parse(calls[2].options.body), { confirmation: "page121", tenant_revision: 7 });
  assert.equal(calls[1].options.headers["Idempotency-Key"], "idem-export");
  assert.equal(calls[2].options.headers["Idempotency-Key"], "idem-delete");
});

test("download requires the fixed zip response contract", async () => {
  const content = new Uint8Array([80, 75, 3, 4]);
  const digest = [...new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", content))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  globalThis.fetch = async () => new Response(content, { status: 200, headers: { "Content-Type": "application/zip", "Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff", "Content-Length": String(content.byteLength), "X-Content-SHA256": digest, "Content-Disposition": `attachment; filename="metahub-tenant-export-${JOB_ID}.zip"` } });
  const result = await downloadTenantExport({ controlApiUrl: "http://control.local", token: "session-token", jobId: JOB_ID });
  assert.equal(result.blob.type, "application/zip");
  assert.equal(result.filename, `metahub-tenant-export-${JOB_ID}.zip`);
});

test("PAGE-121 owner fails closed and renderer mirrors independent permissions", () => {
  const readOnly = context([PERMISSIONS.TENANT_DATA_READ]);
  assert.ok(tenantDataLifecycleOwner(readOnly, shell(readOnly)));
  assert.equal(tenantDataLifecycleOwner(readOnly, shell(readOnly), "delete"), null);
  assert.equal(tenantDataLifecycleOwner(readOnly, { ...shell(readOnly), activeNavigationItemId: "tenant" }), null);

  const html = renderTenantDataLifecycle({ state: { lifecycle_status: "active", revision: 7, cache_generation: 2 }, exports: [exportJob()], deletion_request: null }, readOnly, shell(readOnly));
  assert.match(html, /data-tenant-data-root/);
  assert.match(html, /data-tenant-data-export disabled/);
  assert.doesNotMatch(html, /data-tenant-data-download=/);
  assert.match(html, /data-tenant-data-delete disabled/);
  const denied = renderTenantDataLifecycle({}, context([]), shell(context([])));
  assert.equal(denied, "");
});

test("PAGE-121 organization settings permission opens the lifecycle overview without mutation rights", () => {
  const organizationAdmin = context([PERMISSIONS.TENANT_SETTINGS_READ]);
  assert.ok(tenantDataLifecycleOwner(organizationAdmin, shell(organizationAdmin)));
  assert.equal(tenantDataLifecycleOwner(organizationAdmin, shell(organizationAdmin), "export"), null);
  const html = renderTenantDataLifecycle(
    { state: { lifecycle_status: "active", revision: 1, cache_generation: 0 }, exports: [], deletion_request: null },
    organizationAdmin,
    shell(organizationAdmin),
  );
  assert.match(html, /data-tenant-data-root/);
  assert.match(html, /data-tenant-data-export disabled/);
  assert.doesNotMatch(html, /data-tenant-data-download=/);
  assert.match(html, /data-tenant-data-delete disabled/);
});

test("PAGE-121 state aborts and scrubs data when its owner changes", () => {
  const state = createTenantDataLifecyclePageState();
  const first = tenantDataLifecycleOwner(context(), shell());
  assert.equal(state.enter(first), true);
  const ticket = state.begin();
  state.set({ state: { lifecycle_status: "active", revision: 7, cache_generation: 2 }, exports: [exportJob()] });
  assert.equal(state.snapshot().exports.length, 1);

  const secondContext = context(undefined, "tenant_other", "usr_other");
  const secondShell = shell(secondContext);
  const second = tenantDataLifecycleOwner(secondContext, secondShell);
  assert.equal(state.enter(second), true);
  assert.equal(ticket.controller.signal.aborted, true);
  assert.equal(state.snapshot(), null);
});

test("PAGE-121 keeps read, mutation, and download lanes independent while generations reject late work", () => {
  const state = createTenantDataLifecyclePageState();
  const owner = tenantDataLifecycleOwner(context(), shell());
  state.enter(owner);
  const read = state.beginLane("read", owner);
  const exportLane = state.beginLane("export", tenantDataLifecycleOwner(context(), shell(), "export"));
  assert.equal(read.controller.signal.aborted, false);
  assert.equal(state.isCurrent(read, owner, "read"), true);
  const replacement = state.beginLane("export", tenantDataLifecycleOwner(context(), shell(), "export"));
  assert.equal(exportLane.controller.signal.aborted, true);
  assert.equal(state.isCurrent(exportLane, owner, "export"), false);
  assert.equal(state.isCurrent(replacement, owner, "export"), true);
  state.clear();
  assert.equal(read.controller.signal.aborted, true);
  assert.equal(replacement.controller.signal.aborted, true);
});

test("tenant data DTO rejects unknown fields and missing attachment integrity headers", async () => {
  globalThis.fetch = async (url) => {
    if (String(url).endsWith("/data-management")) return new Response(JSON.stringify({ state: { lifecycle_status: "active", revision: 7, cache_generation: 2, updated_at: "2026-08-18T00:00:00Z" }, exports: [], deletion_request: null, request_id: "req_page121_strict", trace_id: "trc_page121_strict", unexpected: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    return new Response(new Uint8Array([80, 75]), { status: 200, headers: { "Content-Type": "application/zip", "Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff", "Content-Length": "2", "Content-Disposition": `attachment; filename="metahub-tenant-export-${JOB_ID}.zip"` } });
  };
  await assert.rejects(() => getTenantDataManagement({ controlApiUrl: "http://control.local", token: "session-token" }), /tenant_data.response_invalid/);
  await assert.rejects(() => downloadTenantExport({ controlApiUrl: "http://control.local", token: "session-token", jobId: JOB_ID }), /tenant_data.response_invalid/);
});
