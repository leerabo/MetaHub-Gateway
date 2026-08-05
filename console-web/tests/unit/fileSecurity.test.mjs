/**
 * PRD-073 PAGE-103 文件安全报告专项测试。
 * 覆盖严格 DTO、ready 文件筛选、扫描空 body、CSV revoke、owner/generation、权限、异常、隐私 DOM
 * 和 390px 响应式合同；固定样本不包含真实 Prompt、OCR 正文、密钥或对象存储定位信息。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { AuthApiError } from "../../src/lib/auth.js";
import { normalizeFileListResponse } from "../../src/lib/files.js";
import {
  downloadFileSecurityReportCsv,
  getFileSecurityReport,
  listFileSecurityFiles,
  normalizeFileSecurityReportResponse,
  scanFileSecurity,
  triggerFileSecurityReportCsvDownload,
} from "../../src/lib/fileSecurity.js";
import {
  createFileSecurityPageState,
  fileSecurityPageOwner,
  fileSecurityReadIdentitySnapshot,
  fileSecurityScanIdentitySnapshot,
} from "../../src/features/file-security/fileSecurityPageState.js";
import { renderFileSecurity } from "../../src/features/file-security/renderFileSecurity.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  PERMISSIONS,
  withPermissionSnapshot,
} from "./permissionTestContext.mjs";

// 测试资源 ID 均为虚构规范值，只用于验证路径 owner 与跨文件拒绝。
const FILE_ID = `file_${"a".repeat(32)}`;
// 第二个文件 ID 只用于证明响应不能替换路径 owner。
const OTHER_FILE_ID = `file_${"b".repeat(32)}`;
// 报告与 finding 共享 UTC 秒时间，冻结 canonical 时间合同。
const CREATED_AT = "2026-08-05T08:00:00Z";

/** 构造 PAGE-103 真实权限镜像，不根据 role 推导 file.read 或扫描权限。 */
function authContext({
  permissions = [PERMISSIONS.FILE_READ, PERMISSIONS.FILE_SECURITY_SCAN],
  tenantId = "tenant_security_a",
  userId = "usr_security_a",
  sessionId = "ses_security_a",
  fingerprint = "file_security_authz_a",
} = {}) {
  // 顶层和 user 权限镜像由公共测试 helper 同步构造，避免角色名隐式补权。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, slug: "default", display_name: "默认租户" },
    user: {
      tenant_id: tenantId,
      user_id: userId,
      email: "security@example.test",
      role: "security_admin",
    },
    session: { session_id: sessionId, session_revision: 1 },
  }, permissions, { permissionRevision: 9, authorizationFingerprint: fingerprint });
}

/** 构造与后端导航授权镜像一致的独立 file-security 导航。 */
function shellState(context = authContext()) {
  return {
    state: "ok",
    activeNavigationItemId: "file-security",
    navigation: {
      active_navigation_item_id: "file-security",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "file-security",
        target_section: "file-security",
        label: "文件安全报告",
        group: "workspace",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.FILE_READ],
      }],
    },
  };
}

/** 构造 PRD-072 exact-key ready FileInfo，PAGE-103 只复用安全摘要。 */
function fileFixture(overrides = {}) {
  return {
    file_id: FILE_ID,
    original_name: "安全评审.docx",
    file_extension: "docx",
    media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    size_bytes: 4096,
    status: "ready",
    virus_scan_status: "not_configured",
    dlp_scan_status: "clean",
    dlp_action: null,
    match_count: 0,
    failure_error_code: null,
    upload_request_id: "req_file_upload_a",
    extract: {
      file_extract_id: `fext_${"c".repeat(32)}`,
      status: "ready",
      extractor_version: "docx-v1",
      text_size_bytes: 512,
      text_code_points: 300,
      estimated_tokens: 120,
      estimator_version: "file-token-estimator-v1",
      failure_error_code: null,
    },
    created_at: CREATED_AT,
    updated_at: "2026-08-05T08:00:01Z",
    completed_at: "2026-08-05T08:00:01Z",
    ...overrides,
  };
}

/** 构造 files 列表信封；测试 cursor 不进入 PAGE-103 页面状态。 */
function listFixture(overrides = {}) {
  return {
    files: [fileFixture()],
    next_cursor: null,
    request_id: "req_file_security_list",
    trace_id: "trc_file_security_list",
    ...overrides,
  };
}

/** 构造一条不含命中文本的固定 finding。 */
function findingFixture(overrides = {}) {
  return {
    finding_id: `fsfd_${"d".repeat(32)}`,
    detector_code: "prompt.visible_instruction",
    category: "prompt_injection",
    severity: "high",
    confidence_milli: 980,
    source_kind: "visible_text",
    location_kind: "document",
    location_index: null,
    status: "detected",
    created_at: CREATED_AT,
    ...overrides,
  };
}

/** 构造三种终态均可覆盖的严格报告信封。 */
function reportFixture(overrides = {}) {
  return {
    report: {
      scan_id: `fscn_${"e".repeat(32)}`,
      file_id: FILE_ID,
      status: "passed",
      risk_level: "none",
      risk_score: 0,
      coverage_status: "complete",
      model_access_status: "allowed",
      detector_version: "file-security-detector-v1",
      boundary_version: "file-context-boundary-v1",
      ocr_status: "not_required",
      ocr_engine_version: null,
      scanned_page_count: 0,
      scanned_image_count: 0,
      finding_count: 0,
      high_finding_count: 0,
      findings: [],
      created_at: CREATED_AT,
      completed_at: CREATED_AT,
      ...(overrides.report ?? {}),
    },
    reused: false,
    request_id: "req_file_security_report",
    trace_id: "trc_file_security_report",
    ...Object.fromEntries(Object.entries(overrides).filter(([key]) => key !== "report")),
  };
}

/** 构造 fetch 可用 JSON Response，并按统一中间件补链路 Header。 */
function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "X-Request-ID": payload.request_id ?? "req_error",
      "X-Trace-ID": payload.trace_id ?? "trc_error",
    },
  });
}

test("PRD-073 ready list sends only frozen status/limit and no tenant or detector selector", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(listFixture());
  };
  const result = await listFileSecurityFiles({
    controlApiUrl: "http://control.local",
    token: "session-token",
    cursor: "signed.security.cursor-1",
    tenantId: "tenant_injected",
    detectorVersion: "attacker-version",
  });
  assert.equal(result.files[0].status, "ready");
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, "/files");
  assert.equal(url.searchParams.get("status"), "ready");
  assert.equal(url.searchParams.get("limit"), "25");
  assert.equal(url.searchParams.get("cursor"), "signed.security.cursor-1");
  assert.deepEqual([...url.searchParams.keys()].sort(), ["cursor", "limit", "status"]);
  assert.equal(calls[0].options.headers.Authorization, "Bearer session-token");
  assert.equal(calls[0].options.cache, "no-store");
});

test("PRD-073 GET and POST use bounded file path while scan sends no body or Content-Type", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(reportFixture());
  };
  const getResult = await getFileSecurityReport({
    controlApiUrl: "http://control.local",
    token: "read-token",
    fileId: FILE_ID,
    tenantId: "tenant_injected",
  });
  const scanResult = await scanFileSecurity({
    controlApiUrl: "http://control.local",
    token: "scan-token",
    fileId: FILE_ID,
    pattern: "attacker-pattern",
  });
  assert.equal(getResult.report.file_id, FILE_ID);
  assert.equal(scanResult.report.status, "passed");
  assert.equal(calls[0].url, `http://control.local/files/${FILE_ID}/security-report`);
  assert.equal(calls[1].url, `http://control.local/files/${FILE_ID}/security-scan`);
  assert.equal(calls[1].options.method, "POST");
  assert.equal(calls[1].options.body, undefined);
  assert.equal(calls[1].options.headers["Content-Type"], undefined);
  assert.equal(calls[1].options.credentials, "omit");
});

test("PRD-073 malformed and cross-file IDs fail before fetch or strict projection", async (t) => {
  let fetchCount = 0;
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse(reportFixture());
  };
  await assert.rejects(
    getFileSecurityReport({
      controlApiUrl: "http://control.local",
      token: "token",
      fileId: "file_bad/../tenant-b",
    }),
    (error) => error instanceof AuthApiError && error.code === "file.not_found",
  );
  assert.equal(fetchCount, 0);
  assert.equal(normalizeFileSecurityReportResponse(reportFixture(), FILE_ID)?.report.file_id, FILE_ID);
  assert.equal(normalizeFileSecurityReportResponse(reportFixture(), OTHER_FILE_ID), null);
});

test("PRD-073 strict DTO accepts passed blocked failed and freezes every public layer", () => {
  const passed = normalizeFileSecurityReportResponse(reportFixture(), FILE_ID);
  const blockedPayload = reportFixture({
    reused: true,
    report: {
      status: "blocked",
      risk_level: "high",
      risk_score: 80,
      model_access_status: "blocked",
      finding_count: 1,
      high_finding_count: 1,
      findings: [findingFixture()],
    },
  });
  const blocked = normalizeFileSecurityReportResponse(blockedPayload, FILE_ID);
  const failed = normalizeFileSecurityReportResponse(reportFixture({
    report: {
      status: "failed",
      risk_level: "unknown",
      risk_score: null,
      coverage_status: "unavailable",
      model_access_status: "blocked",
      ocr_status: "failed",
    },
  }), FILE_ID);
  assert.equal(passed.report.model_access_status, "allowed");
  assert.equal(blocked.report.findings[0].detector_code, "prompt.visible_instruction");
  assert.equal(blocked.reused, true);
  assert.equal(failed.report.risk_score, null);
  assert.equal(Object.isFrozen(blocked), true);
  assert.equal(Object.isFrozen(blocked.report), true);
  assert.equal(Object.isFrozen(blocked.report.findings), true);
  assert.equal(Object.isFrozen(blocked.report.findings[0]), true);
});

test("PRD-073 strict DTO accepts the pinned real OCR report version and rejects wildcards", () => {
  const ocrPayload = reportFixture({
    report: {
      status: "blocked",
      risk_level: "critical",
      risk_score: 100,
      model_access_status: "blocked",
      ocr_status: "completed",
      ocr_engine_version: "rapidocr-onnxruntime-1.4.4",
      scanned_image_count: 1,
      finding_count: 2,
      high_finding_count: 1,
      findings: [
        findingFixture({
          detector_code: "ocr.prompt_instruction",
          category: "ocr",
          severity: "critical",
          source_kind: "docx_image",
          location_kind: "image",
          location_index: 1,
        }),
        findingFixture({
          finding_id: `fsfd_${"c".repeat(32)}`,
          detector_code: "ocr.text_observed",
          category: "ocr",
          severity: "low",
          confidence_milli: 900,
          source_kind: "docx_image",
          location_kind: "image",
          location_index: 1,
        }),
      ],
    },
  });
  const normalized = normalizeFileSecurityReportResponse(ocrPayload, FILE_ID);
  assert.equal(normalized?.report.ocr_engine_version, "rapidocr-onnxruntime-1.4.4");

  const wildcardVersion = structuredClone(ocrPayload);
  wildcardVersion.report.ocr_engine_version = "rapidocr-onnxruntime-1.4.x";
  assert.equal(normalizeFileSecurityReportResponse(wildcardVersion, FILE_ID), null);
});

test("PRD-073 strict DTO rejects sensitive extras, duplicate findings, unknown rules and false clean states", () => {
  const blocked = reportFixture({
    report: {
      status: "blocked",
      risk_level: "high",
      risk_score: 80,
      model_access_status: "blocked",
      finding_count: 1,
      high_finding_count: 1,
      findings: [findingFixture()],
    },
  });
  const injections = [
    { ...blocked, tenant_id: "tenant_private" },
    { ...blocked, report: { ...blocked.report, report_sha256: "private-hash" } },
    { ...blocked, report: { ...blocked.report, extracted_text: "private-body" } },
    {
      ...blocked,
      report: {
        ...blocked.report,
        findings: [{ ...findingFixture(), evidence_fingerprint: "private-hmac" }],
      },
    },
    {
      ...blocked,
      report: {
        ...blocked.report,
        finding_count: 2,
        high_finding_count: 2,
        findings: [findingFixture(), findingFixture()],
      },
    },
    {
      ...blocked,
      report: {
        ...blocked.report,
        findings: [findingFixture({ detector_code: "custom.attacker_rule" })],
      },
    },
    reportFixture({
      report: {
        status: "failed",
        risk_level: "unknown",
        risk_score: 0,
        coverage_status: "unavailable",
        model_access_status: "blocked",
        ocr_status: "failed",
      },
    }),
    reportFixture({ report: { status: "passed", model_access_status: "blocked" } }),
  ];
  for (const payload of injections) {
    assert.equal(normalizeFileSecurityReportResponse(payload, FILE_ID), null);
  }
});

test("PRD-073 errors expose fixed code text and never server parser details", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async () => jsonResponse({
    code: "file.security_scan_unavailable",
    message: "private.docx leaked parser path D:/service/blob and OCR output",
    request_id: "req_security_failure",
    trace_id: "trc_security_failure",
  }, 503);
  await assert.rejects(
    scanFileSecurity({ controlApiUrl: "http://control.local", token: "token", fileId: FILE_ID }),
    (error) => (
      error instanceof AuthApiError
      && error.code === "file.security_scan_unavailable"
      && error.message === "文件安全扫描暂不可用"
      && error.requestId === "req_security_failure"
      && !error.message.includes("private.docx")
    ),
  );
});

test("PRD-073 CSV client validates exact filename security headers size and bearer", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response("scan_id,status\r\nfscn_test,passed\r\n", {
      status: 200,
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="${FILE_ID}-security-report.csv"`,
        "Cache-Control": "no-store, private",
        "X-Content-Type-Options": "nosniff",
        "X-Request-ID": "req_security_csv",
        "X-Trace-ID": "trc_security_csv",
      },
    });
  };
  const result = await downloadFileSecurityReportCsv({
    controlApiUrl: "http://control.local",
    token: "read-token",
    fileId: FILE_ID,
    tenantId: "tenant_injected",
  });
  assert.equal(result.filename, `${FILE_ID}-security-report.csv`);
  assert.equal(result.requestId, "req_security_csv");
  assert.equal(calls[0].url, `http://control.local/files/${FILE_ID}/security-report.csv`);
  assert.equal(calls[0].options.headers.Authorization, "Bearer read-token");
  assert.equal(calls[0].options.headers.Accept, "text/csv");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].url.includes("tenant"), false);

  globalThis.fetch = async () => new Response("bad", {
    status: 200,
    headers: {
      "Content-Type": "text/csv",
      "Content-Disposition": 'attachment; filename="private-name.csv"',
      "Cache-Control": "public",
      "X-Request-ID": "req_bad_csv",
      "X-Trace-ID": "trc_bad_csv",
    },
  });
  await assert.rejects(
    downloadFileSecurityReportCsv({ controlApiUrl: "http://control.local", token: "token", fileId: FILE_ID }),
    (error) => error instanceof AuthApiError && error.code === "file.security_export_invalid",
  );
});

test("PRD-073 CSV object URL is revoked on both click success and failure", () => {
  const blob = new Blob(["scan_id,status\r\n"], { type: "text/csv" });
  const events = [];
  const link = {
    hidden: false,
    click() { events.push("click"); },
    remove() { events.push("remove"); },
  };
  const environment = {
    URL: {
      createObjectURL(value) { events.push(`create:${value.size}`); return "blob:file-security"; },
      revokeObjectURL(url) { events.push(`revoke:${url}`); },
    },
    document: {
      body: { append(value) { assert.equal(value, link); events.push("append"); } },
      createElement(tag) { assert.equal(tag, "a"); return link; },
    },
  };
  triggerFileSecurityReportCsvDownload({
    state: "ok",
    fileId: FILE_ID,
    blob,
    filename: `${FILE_ID}-security-report.csv`,
  }, environment);
  assert.deepEqual(events, ["create:16", "append", "click", "remove", "revoke:blob:file-security"]);

  const failureEvents = [];
  const failingLink = {
    click() { throw new Error("download blocked"); },
    remove() { failureEvents.push("remove"); },
  };
  assert.throws(() => triggerFileSecurityReportCsvDownload({
    state: "ok",
    fileId: FILE_ID,
    blob,
    filename: `${FILE_ID}-security-report.csv`,
  }, {
    URL: {
      createObjectURL() { return "blob:file-security-failed"; },
      revokeObjectURL(url) { failureEvents.push(`revoke:${url}`); },
    },
    document: {
      body: { append() {} },
      createElement() { return failingLink; },
    },
  }), /download blocked/);
  assert.deepEqual(failureEvents, ["remove", "revoke:blob:file-security-failed"]);
});

test("PRD-073 owner keeps report read and scan permission independent", () => {
  const scanner = authContext();
  const scannerOwner = fileSecurityPageOwner(scanner, shellState(scanner));
  assert.ok(scannerOwner);
  assert.equal(scannerOwner.canScan, true);
  assert.match(fileSecurityReadIdentitySnapshot(scanner), /file\.read/);
  assert.match(fileSecurityScanIdentitySnapshot(scanner), /file\.security\.scan/);

  const readOnly = authContext({
    permissions: [PERMISSIONS.FILE_READ],
    fingerprint: "file_security_read_only",
  });
  const readOwner = fileSecurityPageOwner(readOnly, shellState(readOnly));
  assert.ok(readOwner);
  assert.equal(readOwner.canScan, false);
  assert.equal(fileSecurityPageOwner(readOnly, shellState(readOnly), { requireScan: true }), null);

  const scanOnly = authContext({
    permissions: [PERMISSIONS.FILE_SECURITY_SCAN],
    fingerprint: "file_security_scan_only",
  });
  assert.equal(fileSecurityPageOwner(scanOnly, shellState(scanOnly)), null);
  const wrongRequired = shellState(scanner);
  wrongRequired.navigation.navigation_items[0].required_permissions = [
    PERMISSIONS.FILE_READ,
    PERMISSIONS.FILE_SECURITY_SCAN,
  ];
  assert.equal(fileSecurityPageOwner(scanner, wrongRequired), null);
});

test("PRD-073 state aborts late owner operations and never exposes owner controller cursor or Blob", () => {
  const context = authContext();
  const owner = fileSecurityPageOwner(context, shellState(context));
  const state = createFileSecurityPageState();
  assert.equal(state.enter(owner), true);
  const load = state.beginLoadOperation(owner, 7);
  const files = normalizeFileListResponse(listFixture({ next_cursor: "private.cursor.signature" }));
  assert.equal(state.acceptFiles(load, files, owner, 7), true);
  assert.equal(state.snapshot().canNext, true);
  const report = normalizeFileSecurityReportResponse(reportFixture(), FILE_ID);
  assert.equal(state.acceptReport(load, report, owner, 7), true);
  state.finishLoadOperation(load);
  const scan = state.beginScanOperation(owner);
  assert.ok(scan);

  const changed = authContext({
    tenantId: "tenant_security_b",
    userId: "usr_security_b",
    sessionId: "ses_security_b",
    fingerprint: "file_security_authz_b",
  });
  const changedOwner = fileSecurityPageOwner(changed, shellState(changed));
  assert.equal(state.enter(changedOwner), true);
  assert.equal(scan.controller.signal.aborted, true);
  assert.equal(state.isScanOperationCurrent(scan, changedOwner), false);
  const serialized = JSON.stringify(state.snapshot());
  assert.equal(serialized.includes("private.cursor.signature"), false);
  assert.equal(serialized.includes(context.authorization_fingerprint), false);
  assert.equal(serialized.includes("controller"), false);
  assert.equal(serialized.includes("Blob"), false);
  assert.equal(state.snapshot().files.length, 0);
});

test("PRD-073 paging restores only private cursor stack and clears the previous report", () => {
  const context = authContext();
  const owner = fileSecurityPageOwner(context, shellState(context));
  const state = createFileSecurityPageState();
  state.enter(owner);
  const firstLoad = state.beginLoadOperation(owner, 1);
  state.acceptFiles(
    firstLoad,
    normalizeFileListResponse(listFixture({ next_cursor: "signed.security.next-1" })),
    owner,
    1,
  );
  state.acceptReport(firstLoad, normalizeFileSecurityReportResponse(reportFixture(), FILE_ID), owner, 1);
  state.finishLoadOperation(firstLoad);
  assert.equal(state.nextPage(), true);
  assert.deepEqual(state.query(), { cursor: "signed.security.next-1" });
  assert.equal(state.snapshot().report, null);
  assert.equal(state.snapshot().canPrevious, true);
  assert.equal(JSON.stringify(state.snapshot()).includes("signed.security.next-1"), false);

  const secondLoad = state.beginLoadOperation(owner, 2);
  state.acceptFiles(secondLoad, normalizeFileListResponse(listFixture({ next_cursor: null })), owner, 2);
  state.finishLoadOperation(secondLoad);
  assert.equal(state.previousPage(), true);
  assert.deepEqual(state.query(), { cursor: null });
});

test("PRD-073 scan and export state preserve only fixed feedback and report facts", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = fileSecurityPageOwner(context, shell);
  const state = createFileSecurityPageState();
  state.enter(owner);
  const load = state.beginLoadOperation(owner, 1);
  state.acceptFiles(load, normalizeFileListResponse(listFixture()), owner, 1);
  state.acceptReportNotFound(load, owner, 1);
  state.finishLoadOperation(load);

  const scan = state.beginScanOperation(owner);
  const blocked = normalizeFileSecurityReportResponse(reportFixture({
    reused: true,
    report: {
      status: "blocked",
      risk_level: "high",
      risk_score: 80,
      model_access_status: "blocked",
      finding_count: 1,
      high_finding_count: 1,
      findings: [findingFixture()],
    },
  }), FILE_ID);
  assert.equal(state.acceptScan(scan, blocked, owner), true);
  state.finishScanOperation(scan);
  assert.equal(state.snapshot().feedback.code, "file.security_scan_reused");

  const exportOperation = state.beginExportOperation(owner);
  const exportResult = {
    state: "ok",
    fileId: FILE_ID,
    blob: new Blob(["private csv bytes"]),
    filename: `${FILE_ID}-security-report.csv`,
    requestId: "req_export",
  };
  assert.equal(state.acceptExport(exportOperation, exportResult, owner), true);
  assert.equal(JSON.stringify(state.snapshot()).includes("private csv bytes"), false);
  assert.equal(state.snapshot().feedback.code, "file.security_report_exported");
});

test("PRD-073 refresh preserves valid selection and clears feedback when its file leaves the page", () => {
  const context = authContext();
  const owner = fileSecurityPageOwner(context, shellState(context));
  const state = createFileSecurityPageState();
  const secondFile = fileFixture({
    file_id: OTHER_FILE_ID,
    original_name: "离线 OCR 评审.docx",
    extract: {
      ...fileFixture().extract,
      file_extract_id: `fext_${"f".repeat(32)}`,
    },
  });
  state.enter(owner);
  const initialLoad = state.beginLoadOperation(owner, 1);
  state.acceptFiles(initialLoad, normalizeFileListResponse(listFixture({
    files: [fileFixture(), secondFile],
  })), owner, 1);
  state.finishLoadOperation(initialLoad);
  assert.equal(state.selectFile(OTHER_FILE_ID), true);

  const scan = state.beginScanOperation(owner);
  assert.equal(state.acceptScan(scan, normalizeFileSecurityReportResponse(reportFixture({
    reused: true,
    report: { file_id: OTHER_FILE_ID },
  }), OTHER_FILE_ID), owner), true);
  state.finishScanOperation(scan);

  // 扫描完成后的全页刷新可能改变后端列表顺序；只要同一 owner 的文件仍在当前页，
  // 就必须同时保留选中 ID 和固定反馈，不能静默跳到首项并让反馈与报告对象错配。
  const refreshedLoad = state.beginLoadOperation(owner, 2);
  assert.equal(state.acceptFiles(refreshedLoad, normalizeFileListResponse(listFixture({
    files: [secondFile, fileFixture()],
  })), owner, 2), true);
  assert.equal(state.selectedFile(), OTHER_FILE_ID);
  assert.equal(state.snapshot().selectedFileId, OTHER_FILE_ID);
  assert.equal(state.snapshot().feedback.code, "file.security_scan_reused");

  state.finishLoadOperation(refreshedLoad);
  const fallbackLoad = state.beginLoadOperation(owner, 3);
  assert.equal(state.acceptFiles(fallbackLoad, normalizeFileListResponse(listFixture({
    files: [fileFixture()],
    next_cursor: "signed.security.next-2",
  })), owner, 3), true);
  assert.equal(state.snapshot().selectedFileId, FILE_ID);
  assert.equal(state.snapshot().feedback, null);
  state.finishLoadOperation(fallbackLoad);

  const firstFileScan = state.beginScanOperation(owner);
  assert.equal(state.acceptScan(firstFileScan, normalizeFileSecurityReportResponse(reportFixture({
    reused: true,
  }), FILE_ID), owner), true);
  state.finishScanOperation(firstFileScan);
  assert.equal(state.snapshot().feedback.code, "file.security_scan_reused");
  assert.equal(state.nextPage(), true);
  assert.equal(state.snapshot().feedback, null);
});

test("PRD-073 renderer escapes filename, shows blocked report and exposes no sensitive contract fields", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = fileSecurityPageOwner(context, shell);
  const state = createFileSecurityPageState();
  state.enter(owner);
  const load = state.beginLoadOperation(owner, 1);
  const files = normalizeFileListResponse(listFixture({
    files: [fileFixture({ original_name: "<img src=x onerror=securityCanary()>.docx" })],
  }));
  state.acceptFiles(load, files, owner, 1);
  const blocked = normalizeFileSecurityReportResponse(reportFixture({
    report: {
      status: "blocked",
      risk_level: "high",
      risk_score: 80,
      model_access_status: "blocked",
      finding_count: 1,
      high_finding_count: 1,
      findings: [findingFixture()],
    },
  }), FILE_ID);
  state.acceptReport(load, blocked, owner, 1);
  const html = renderFileSecurity(state.snapshot(), context, shell);
  assert.match(html, /&lt;img src=x onerror=securityCanary\(\)&gt;\.docx/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /禁止进入模型上下文/);
  assert.match(html, /prompt\.visible_instruction/);
  assert.match(html, /data-file-security-scan/);
  assert.match(html, /data-file-security-export/);
  assert.doesNotMatch(html, /tenant_id|actor_user_id|original_object_key|extracted_object_key|evidence_fingerprint|report_sha256|extracted_text/);
  assert.doesNotMatch(html, /data-file-name=/);
});

test("PRD-073 read-only renderer creates CSV for existing report but no scan command", () => {
  const context = authContext({
    permissions: [PERMISSIONS.FILE_READ],
    fingerprint: "file_security_read_only_render",
  });
  const shell = shellState(context);
  const owner = fileSecurityPageOwner(context, shell);
  const state = createFileSecurityPageState();
  state.enter(owner);
  const load = state.beginLoadOperation(owner, 1);
  state.acceptFiles(load, normalizeFileListResponse(listFixture()), owner, 1);
  state.acceptReport(load, normalizeFileSecurityReportResponse(reportFixture(), FILE_ID), owner, 1);
  const html = renderFileSecurity(state.snapshot(), context, shell);
  assert.match(html, /只读报告视图/);
  assert.match(html, /data-file-security-export/);
  assert.doesNotMatch(html, /data-file-security-scan/);
});

test("PRD-073 authorized navigation renders PAGE-103 through the existing console shell", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = fileSecurityPageOwner(context, shell);
  const state = createFileSecurityPageState();
  state.enter(owner);
  const load = state.beginLoadOperation(owner, 1);
  state.acceptFiles(load, normalizeFileListResponse(listFixture()), owner, 1);
  state.acceptReport(load, normalizeFileSecurityReportResponse(reportFixture(), FILE_ID), owner, 1);
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: context,
    consoleShellState: shell,
    fileSecurityState: state.snapshot(),
  });
  assert.match(container.innerHTML, /data-active-navigation="file-security"/);
  assert.match(container.innerHTML, /class="panel file-security-panel"/);
  assert.match(container.innerHTML, /data-file-security-root/);
  assert.match(container.innerHTML, /安全评审\.docx/);
});

test("PRD-073 main lifecycle and narrow CSS freeze cleanup, permissions and no horizontal table fallback", async () => {
  const [mainSource, shellSource, permissionsSource, clientSource, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/permissions.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/fileSecurity.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /loadFileSecurityState\(consoleShellState, refreshId\)/);
  assert.match(mainSource, /bindFileSecurityPageInteractions\(consoleShellState\)/);
  assert.match(mainSource, /clearFileSecurityStateOnIdentityChange/);
  assert.match(mainSource, /window\.addEventListener\("pagehide"[\s\S]*fileSecurityPageState\.clear\(\)/);
  assert.match(mainSource, /logoutButton[\s\S]*fileSecurityPageState\.clear\(\)/);
  assert.match(mainSource, /itemId !== "file-security"[\s\S]*fileSecurityPageState\.clear\(\)/);
  assert.match(mainSource, /fileSecurityPageState\.isActionBusy\(\)/);
  assert.match(mainSource, /error\.status === 403[\s\S]*error\.code === "auth\.permission_denied"/);
  const loaderSource = mainSource.match(
    /async function loadFileSecurityState[\s\S]*?\n}\n\/\*\*\n \* 清除预算页会话内快照/,
  )?.[0] ?? "";
  // 每个 await 的成功和异常落盘路径都必须独立核对全局 refresh generation；
  // 断言限定在 PAGE-103 loader 内，避免被函数入口或其他页面的同名门禁误满足。
  assert.match(loaderSource, /await listFileSecurityFiles\([\s\S]*?if \(refreshId !== refreshSequence\) return null;[\s\S]*?acceptFiles/);
  assert.match(loaderSource, /await getFileSecurityReport\([\s\S]*?if \(refreshId !== refreshSequence\) return null;[\s\S]*?acceptReport/);
  assert.match(loaderSource, /catch \(error\) \{[\s\S]*?refreshId !== refreshSequence[\s\S]*?reportOwner/);
  assert.match(loaderSource, /catch \(error\) \{[\s\S]*?refreshId !== refreshSequence[\s\S]*?currentOwner/);
  assert.equal((loaderSource.match(/refreshId !== refreshSequence/g) ?? []).length, 5);
  assert.match(shellSource, /case "file-security":/);
  assert.match(permissionsSource, /FILE_SECURITY_SCAN: "file\.security\.scan"/);
  assert.match(clientSource, /link\.remove\?\.\(\);[\s\S]*revokeObjectURL\(objectUrl\)/);
  assert.match(css, /\.file-security-layout\s*\{[\s\S]*grid-template-columns:/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.file-security-layout\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /\.file-security-finding-row:not\(\.is-head\) > \[data-label\]::before/);
  assert.doesNotMatch(css.match(/@media \(max-width: 720px\)[\s\S]*?\n}\n\n\/\* PAGE-093/)?.[0] ?? "", /overflow-x:\s*auto/);
  assert.equal((css.match(/{/g) ?? []).length, (css.match(/}/g) ?? []).length);
});
