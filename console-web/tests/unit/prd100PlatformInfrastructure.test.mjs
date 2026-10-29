import assert from "node:assert/strict";
import test from "node:test";

import {
  createPlatformBackup,
  getPlatformBackup,
  getPlatformInfrastructureStatus,
} from "../../src/lib/platformInfrastructure.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";

const CONTROL_URL = "http://control.test";
const BACKUP_ID = `bkp_${"1".repeat(32)}`;

test("Infrastructure client validates exact resource and backup projections", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse(statusPayload());
  try {
    assert.equal((await getPlatformInfrastructureStatus({
      controlApiUrl: CONTROL_URL,
    })).overall_status, "healthy");

    globalThis.fetch = async () => jsonResponse({ ...statusPayload(), database_url: "private" });
    await assert.rejects(
      getPlatformInfrastructureStatus({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "infrastructure.response_invalid",
    );

    const wrongProvider = statusPayload();
    wrongProvider.resources[0].provider_code = "redis";
    globalThis.fetch = async () => jsonResponse(wrongProvider);
    await assert.rejects(
      getPlatformInfrastructureStatus({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "infrastructure.response_invalid",
    );

    globalThis.fetch = async () => jsonResponse({
      ...backupPayload(),
      backup: { ...backupPayload().backup, object_key: "backups/private.mhb" },
    });
    await assert.rejects(
      getPlatformBackup({ controlApiUrl: CONTROL_URL, backupId: BACKUP_ID }),
      (error) => error.code === "infrastructure.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Backup creation binds platform CSRF and idempotency without bearer credentials", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  const csrf = "c".repeat(43);
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${csrf}` };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(backupPayload(), 201);
  };
  try {
    await createPlatformBackup({
      controlApiUrl: CONTROL_URL,
      backupType: "full",
      retentionDays: 30,
      idempotencyKey: "platform-backup-test-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${CONTROL_URL}/platform/backups`);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], csrf);
  assert.equal(calls[0].options.headers["Idempotency-Key"], "platform-backup-test-0001");
  assert.equal("Authorization" in calls[0].options.headers, false);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    backup_type: "full",
    scope_kind: "platform",
    workspace_id: null,
    tenant_id: null,
    retention_days: 30,
    confirmed: true,
  });
});

test("Infrastructure rendering prunes backup controls and never renders storage identifiers", () => {
  const malicious = `<img src=x onerror="private-value">`;
  const writable = { innerHTML: "" };
  const writableState = platformState([
    "platform.infrastructure.read",
    "platform.backup.create",
  ]);
  writableState.infrastructure.resources[0].version_summary = malicious;
  writableState.backup.backup.object_key = "backups/private-object.mhb";
  writableState.backup.backup.kms_key_id = "private-kms-identifier";
  renderPlatformWorkspace(writable, writableState);

  assert.match(writable.innerHTML, /data-platform-backup-form/u);
  assert.match(writable.innerHTML, /&lt;img src=x onerror=&quot;private-value&quot;&gt;/u);
  assert.doesNotMatch(writable.innerHTML, /<img|private-object|private-kms-identifier/u);

  const readOnly = { innerHTML: "" };
  renderPlatformWorkspace(readOnly, platformState(["platform.infrastructure.read"]));
  assert.match(readOnly.innerHTML, /组件状态/u);
  assert.doesNotMatch(readOnly.innerHTML, /data-platform-backup-form/u);
});

function platformState(permissions) {
  return {
    session: {
      context: {
        expires_at: "2026-08-23T00:00:00Z",
        permissions,
        operator: { display_name: "Operations", roles: ["platform_ops"] },
      },
    },
    navigation: [{ id: "infrastructure", label: "Infrastructure", href: "/platform.html#infrastructure" }],
    view: "infrastructure",
    infrastructure: statusPayload(),
    backup: backupPayload(),
    loading: false,
  };
}

function statusPayload() {
  return {
    resources: [
      resource("database_cluster", "postgresql", "postgresql-16.4"),
      resource("cache_cluster", "redis", "redis-7.4"),
      resource("object_store", "s3_compatible", "s3-compatible"),
      resource("key_ring", "kms", "kms-managed"),
    ],
    overall_status: "healthy",
    rpo_minutes: 15,
    rto_minutes: 120,
    request_id: "req-infrastructure",
    trace_id: "trc-infrastructure",
  };
}

function resource(resourceKind, providerCode, versionSummary) {
  return {
    resource_kind: resourceKind,
    provider_code: providerCode,
    status: "healthy",
    version_summary: versionSummary,
    latency_ms: 12,
    verified: true,
    last_checked_at: "2026-08-22T04:00:00Z",
    last_verified_at: "2026-08-22T04:00:00Z",
    error_code: null,
  };
}

function backupPayload() {
  return {
    backup: {
      backup_id: BACKUP_ID,
      backup_type: "full",
      scope_kind: "platform",
      workspace_id: null,
      tenant_id: null,
      status: "ready",
      source_backend: "postgresql",
      source_watermark: "postgresql-lsn:0/1234",
      size_bytes: 4096,
      content_sha256: "a".repeat(64),
      manifest_sha256: "b".repeat(64),
      kms_key_version: "kmsv_safe-version",
      schema_head: "0092",
      error_code: null,
      retention_until: "2026-09-22T04:00:00Z",
      created_at: "2026-08-22T04:00:00Z",
      updated_at: "2026-08-22T04:02:00Z",
      completed_at: "2026-08-22T04:02:00Z",
      verification: {
        status: "passed",
        schema_head: "0092",
        integrity_status: "ok",
        manifest_sha256: "b".repeat(64),
        tenant_owner_sample_count: 1,
        error_code: null,
        started_at: "2026-08-22T04:01:00Z",
        completed_at: "2026-08-22T04:02:00Z",
      },
    },
    idempotent_replay: false,
    request_id: "req-backup",
    trace_id: "trc-backup",
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(payload); },
  };
}
