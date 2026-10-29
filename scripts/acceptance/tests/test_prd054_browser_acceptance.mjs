/**
 * PRD-054 Browser 验收器的纯合同测试。
 *
 * 本文件不启动服务、浏览器或 Playwright，只验证 Runtime 前置证据、Explain exact DTO、
 * 回环网络边界、双视口布局、合成 PNG、隐私和 Session 清理合同。
 */

import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

import {
  BrowserAcceptanceFailure,
  assertBrowserPrivacy,
  assertLayoutMetrics,
  assertSafeEvidence,
  assertViewportIntersection,
  browserRequestAllowed,
  inspectPngPixels,
  normalizeLoopbackOrigin,
  readEnvironmentIdentity,
  resolveArtifactDirectory,
  responseEvidence,
  screenshotFilename,
  validateExplanationProjection,
  validateRuntimeEvidence,
  verifySessionCleanup,
} from "../prd054-browser-acceptance.mjs";


// 所有 fixture 只使用公开不透明 ID，不包含任何可用 Token、Key、Prompt 或 Response。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const COMPLETE_REQUEST_ID = "req_prd054_complete_browser_unit";
const PARTIAL_REQUEST_ID = "req_prd054_partial_browser_unit";
const REQUEST_ID = "req_prd054_browser_operation";
const TRACE_ID = "trc_prd054_browser_operation";
const STAGES = [
  "authentication", "rate_limit", "budget", "dlp", "policy", "routing",
  "keypool", "provider", "usage_and_finops", "logs_and_webhooks",
];
const SECTIONS = [
  "authentication", "rate_limit", "budget", "dlp", "policy", "routing",
  "keypool", "provider_response", "usage_and_finops", "logs", "webhooks",
];


/** 构造 Browser 前置所需的最小、完整 Runtime 通过证据。 */
function runtimeEvidence() {
  const http = (status, code = undefined) => ({
    status,
    request_id: `${REQUEST_ID}_${status}_${code ? "error" : "ok"}`,
    trace_id: `${TRACE_ID}_${status}_${code ? "error" : "ok"}`,
    ...(code ? { code } : {}),
  });
  return {
    schema_version: 1,
    prd_id: "PRD-054",
    status: "passed",
    generated_resources: {
      tenant_id: "tenant_browser_unit",
      complete_request_id: COMPLETE_REQUEST_ID,
      partial_request_id: PARTIAL_REQUEST_ID,
    },
    checks: {
      database: { migration_0044_applied: true, integrity_verified: true },
      services: { gateway_http_200: true, control_http_200: true, console_http_200: true },
      roles: { developer_session_bound: true, viewer_session_bound: true },
      api: {
        admin_allowed: true,
        developer_allowed: true,
        viewer_denied_before_input: true,
        cross_tenant_same_shape_404: true,
        complete_contract: { status: "complete", section_count: 11, decision_stage_count: 10 },
        partial_contract: { status: "partial", section_count: 11, decision_stage_count: 10 },
      },
      outbox: {
        source_outbox_persisted: true,
        stable_event_ids_unique: true,
        failed_request_fact_count: 0,
      },
      cleanup: {
        admin_session_revoked: true,
        developer_session_revoked: true,
        viewer_session_revoked: true,
        session_revoked: true,
      },
    },
    http: {
      admin_complete: http(200),
      developer_complete: http(200),
      admin_partial: http(200),
      viewer_malformed: http(403, "auth.permission_denied"),
      cross_tenant: http(404, "request_explanation.not_found"),
      invalid_query: http(422, "request_explanation.request_invalid"),
    },
  };
}


/** 构造固定十阶段、十一区块和可选 partial 缺口的 Explain 响应。 */
function explanationPayload(status = "complete") {
  const sections = Object.fromEntries(SECTIONS.map((name) => [name, {
    evidence_state: status === "partial" && name === "routing" ? "not_recorded" : "not_applicable",
    reason_code: `request_explanation.${name}_unit`,
    facts: [],
    truncated: false,
  }]));
  sections.rate_limit = {
    evidence_state: "complete",
    reason_code: "gateway.rate_limit_exceeded",
    facts: [{
      kind: "rate_limit",
      fact_id: null,
      occurred_at: "2026-07-28T00:00:00Z",
      values: { decision: "blocked", error_code: "gateway.rate_limit_exceeded" },
    }],
    truncated: false,
  };
  const sourceRequestId = status === "complete" ? COMPLETE_REQUEST_ID : PARTIAL_REQUEST_ID;
  return {
    request_id: REQUEST_ID,
    trace_id: TRACE_ID,
    request_explanation: {
      request_explanation_id: `rxe_${"1".repeat(32)}`,
      source_request_id: sourceRequestId,
      source_trace_id: "trc_prd054_source_unit",
      status,
      evidence_version: "request_explanation_v1",
      captured_at: "2026-07-28T00:00:01Z",
      summary: {
        estimated_cost_amount: "12345678901234567890.123456",
      },
      decision_chain: STAGES.map((stage) => ({
        stage,
        status: "not_evaluated",
        evidence_state: "not_applicable",
        reason_code: `request_explanation.${stage}_unit`,
        occurred_at: null,
        evidence_refs: [],
      })),
      ...sections,
      missing_sections: status === "partial" ? ["routing"] : [],
      truncated_sections: [],
      view_event_status: "pending",
    },
  };
}


/** 构造像素检查器可解析的非单色 PNG，用于证明截图尺寸与内容门禁。 */
function syntheticPng() {
  /** 构造带长度和校验占位的 PNG 数据块，测试不依赖外部图片文件。 */
  const chunk = (type, data) => {
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    return Buffer.concat([length, Buffer.from(type, "ascii"), data, Buffer.alloc(4)]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(2, 0);
  header.writeUInt32BE(2, 4);
  header[8] = 8;
  header[9] = 2;
  header[10] = 0;
  header[11] = 0;
  header[12] = 0;
  const raw = Buffer.from([
    0, 255, 255, 255, 0, 0, 0,
    0, 0, 120, 220, 240, 20, 20,
  ]);
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}


test("loopback, artifact and browser network boundaries fail closed", () => {
  assert.equal(normalizeLoopbackOrigin("http://127.0.0.1:15173/", "console"), "http://127.0.0.1:15173");
  for (const unsafe of [
    "https://localhost:5173",
    "http://example.com:5173",
    "http://user:pass@localhost:5173",
    "http://localhost:5173/path",
    "http://localhost",
  ]) assert.throws(() => normalizeLoopbackOrigin(unsafe, "console"), BrowserAcceptanceFailure);

  const batch = path.join(ROOT, "artifacts", "prd054", "unit-batch01");
  assert.equal(resolveArtifactDirectory(batch, ROOT), batch);
  assert.throws(
    () => resolveArtifactDirectory(path.join(ROOT, "artifacts", "prd053", "unit-batch01"), ROOT),
    BrowserAcceptanceFailure,
  );
  const allowed = new Set(["http://localhost:5173", "http://localhost:8001", "http://localhost:8000"]);
  assert.equal(browserRequestAllowed("http://localhost:5173/assets/main.js?v=1", allowed), true);
  assert.equal(browserRequestAllowed("http://localhost:8001/request-explain/req_unit", allowed), true);
  assert.equal(browserRequestAllowed("http://localhost:8001/request-explain/req_unit?tenant_id=forged", allowed), false);
  assert.equal(browserRequestAllowed("https://example.com/private", allowed), false);
});


test("environment identity is removed before browser launch", () => {
  const env = {
    METAHUB_PRD054_ADMIN_PASSWORD: "BrowserUnit!2026",
    METAHUB_PRD054_ADMIN_EMAIL: "Admin@MetaHub.Local",
    METAHUB_PRD054_TENANT_SLUG: "default",
  };
  assert.deepEqual(readEnvironmentIdentity(env), {
    adminPassword: "BrowserUnit!2026",
    adminEmail: "admin@metahub.local",
    tenantSlug: "default",
  });
  assert.equal("METAHUB_PRD054_ADMIN_PASSWORD" in env, false);
  assert.equal("METAHUB_PRD054_ADMIN_EMAIL" in env, false);
});


test("runtime evidence requires migration, RBAC, DTO, outbox and cleanup", () => {
  assert.deepEqual(validateRuntimeEvidence(runtimeEvidence()), {
    completeRequestId: COMPLETE_REQUEST_ID,
    partialRequestId: PARTIAL_REQUEST_ID,
    tenantId: "tenant_browser_unit",
  });
  const missingOutbox = structuredClone(runtimeEvidence());
  missingOutbox.checks.outbox.source_outbox_persisted = false;
  assert.throws(() => validateRuntimeEvidence(missingOutbox), BrowserAcceptanceFailure);
  const activeSession = structuredClone(runtimeEvidence());
  activeSession.checks.cleanup.viewer_session_revoked = false;
  assert.throws(() => validateRuntimeEvidence(activeSession), BrowserAcceptanceFailure);
});


test("complete and partial projections enforce exact DTO and string precision", () => {
  assert.deepEqual(
    validateExplanationProjection(explanationPayload("complete"), COMPLETE_REQUEST_ID, "complete"),
    { status: "complete", decision_stage_count: 10, section_count: 11, privacy_verified: true },
  );
  assert.equal(
    validateExplanationProjection(explanationPayload("partial"), PARTIAL_REQUEST_ID, "partial").status,
    "partial",
  );
  const extraField = structuredClone(explanationPayload("complete"));
  extraField.request_explanation.rate_limit.facts[0].values.raw_usage = "private";
  assert.throws(
    () => validateExplanationProjection(extraField, COMPLETE_REQUEST_ID, "complete"),
    BrowserAcceptanceFailure,
  );
  const wrongOrder = structuredClone(explanationPayload("complete"));
  wrongOrder.request_explanation.decision_chain.reverse();
  assert.throws(
    () => validateExplanationProjection(wrongOrder, COMPLETE_REQUEST_ID, "complete"),
    BrowserAcceptanceFailure,
  );
});


test("response evidence binds body, headers and no-store", () => {
  const response = {
    status: () => 200,
    headers: () => ({
      "x-request-id": REQUEST_ID,
      "x-trace-id": TRACE_ID,
      "cache-control": "no-store, private",
      pragma: "no-cache",
      vary: "Origin, Authorization",
    }),
  };
  assert.deepEqual(responseEvidence(response, { request_id: REQUEST_ID, trace_id: TRACE_ID }, "unit.response"), {
    status: 200,
    request_id: REQUEST_ID,
    trace_id: TRACE_ID,
    no_store: true,
  });
  assert.throws(
    () => responseEvidence(response, { request_id: "req_other", trace_id: TRACE_ID }, "unit.response"),
    BrowserAcceptanceFailure,
  );
});


test("desktop columns and mobile stack reject overflow, clipping and overlap", () => {
  const base = {
    document_scroll_width: 390,
    body_scroll_width: 390,
    root_left: 0,
    root_right: 390,
    root_client_width: 390,
    root_scroll_width: 390,
    clipped_control_count: 0,
    clipped_content_count: 0,
    horizontal_container_overflow_count: 0,
    control_overlap_count: 0,
  };
  const mobile = {
    ...base,
    viewport_width: 390,
    viewport_height: 844,
    summary_box: { left: 8, top: 100, right: 382, bottom: 300 },
    evidence_box: { left: 8, top: 300, right: 382, bottom: 800 },
  };
  assert.equal(assertLayoutMetrics(mobile, { width: 390, height: 844, mode: "stack" }).responsive_mode, "stack");
  const desktop = {
    ...base,
    viewport_width: 1280,
    viewport_height: 720,
    document_scroll_width: 1280,
    body_scroll_width: 1280,
    root_right: 1280,
    root_client_width: 1280,
    root_scroll_width: 1280,
    summary_box: { left: 200, top: 100, right: 500, bottom: 650 },
    evidence_box: { left: 500, top: 100, right: 1270, bottom: 900 },
  };
  assert.equal(assertLayoutMetrics(desktop, { width: 1280, height: 720, mode: "columns" }).responsive_mode, "columns");
  assert.deepEqual(assertViewportIntersection({ x: 1, y: 2, width: 200, height: 40 }, { width: 390, height: 844 }), {
    anchor_intersects_viewport: true,
  });
  assert.throws(
    () => assertLayoutMetrics({ ...mobile, document_scroll_width: 500 }, { width: 390, height: 844, mode: "stack" }),
    BrowserAcceptanceFailure,
  );
});


test("screenshot naming and PNG pixels prove exact viewport and nonblank output", () => {
  assert.equal(screenshotFilename("complete", "desktop-1280x720"), "screenshots/complete-desktop-1280x720.png");
  assert.throws(() => screenshotFilename("unknown", "desktop-1280x720"), BrowserAcceptanceFailure);
  const inspected = inspectPngPixels(syntheticPng(), { width: 2, height: 2 });
  assert.equal(inspected.width, 2);
  assert.equal(inspected.height, 2);
  assert.equal(inspected.non_blank_pixels, true);
  assert.match(inspected.sha256, /^[0-9a-f]{64}$/);
  assert.throws(() => inspectPngPixels(syntheticPng(), { width: 3, height: 2 }), BrowserAcceptanceFailure);
});


test("privacy and session cleanup expose only zero-count evidence", () => {
  assert.deepEqual(assertBrowserPrivacy({
    local_storage_count: 0,
    session_auth_slot_count: 2,
    forbidden_storage_key_count: 0,
    request_id_url_or_storage_hits: 0,
    private_fixture_hits: 0,
    bearer_dom_hits: 0,
    auth_token_exposed: false,
  }), {
    source_id_persistence_hits: 0,
    private_value_hits: 0,
    auth_storage_contract_valid: true,
  });
  assert.deepEqual(verifySessionCleanup({
    local_storage_count: 0,
    session_storage_count: 0,
    explanation_dom_count: 0,
    source_id_dom_hits: 0,
  }), {
    storage_cleared: true,
    explanation_dom_cleared: true,
    source_id_cleared: true,
  });
  assert.throws(() => verifySessionCleanup({
    local_storage_count: 0,
    session_storage_count: 1,
    explanation_dom_count: 0,
    source_id_dom_hits: 0,
  }), BrowserAcceptanceFailure);
});


test("final evidence rejects sensitive slots, patterns and known values", () => {
  assert.equal(assertSafeEvidence({ prd_id: "PRD-054", request_id: REQUEST_ID }, []), true);
  assert.throws(() => assertSafeEvidence({ password: "plain" }, []), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ status: "Bearer opaque-session-value" }, []), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence({ status: "prefix-known-value-suffix" }, ["known-value"]), BrowserAcceptanceFailure);
});
