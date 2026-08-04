/**
 * PRD-072 文件工作台专项测试。
 * 覆盖 strict DTO、multipart 单文件合同、owner/generation、读写权限投影、XSS 与响应式布局；
 * 测试数据不包含真实正文、密钥、DLP pattern 或对象存储定位信息。
 */

import assert from "node:assert/strict";
import { File } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { AuthApiError } from "../../src/lib/auth.js";
import {
  FILE_UPLOAD_MAX_BYTES,
  getFile,
  isSupportedUploadName,
  listFiles,
  normalizeFileListResponse,
  normalizeFileResponse,
  uploadFile,
} from "../../src/lib/files.js";
import {
  createFilePageState,
  filePageOwner,
  fileReadIdentitySnapshot,
  fileWriteIdentitySnapshot,
} from "../../src/features/files/filePageState.js";
import { renderFiles } from "../../src/features/files/renderFiles.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  PERMISSIONS,
  withPermissionSnapshot,
} from "./permissionTestContext.mjs";

/** 构造包含真实授权镜像的文件页面身份。 */
function authContext({
  permissions = [PERMISSIONS.FILE_READ, PERMISSIONS.FILE_WRITE],
  tenantId = "tenant_files_a",
  userId = "usr_files_a",
  sessionId = "ses_files_a",
  fingerprint = "files_authz_a",
} = {}) {
  // 测试身份同时写入顶层与 user 镜像，确保 owner 不依赖角色名补权。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, slug: "default" },
    user: {
      tenant_id: tenantId,
      user_id: userId,
      email: "files@example.test",
      role: "developer",
    },
    session: { session_id: sessionId, session_revision: 1 },
  }, permissions, { permissionRevision: 8, authorizationFingerprint: fingerprint });
}

/** 构造与后端 console navigation 镜像一致的 files 导航。 */
function shellState(context = authContext()) {
  return {
    state: "ok",
    activeNavigationItemId: "files",
    navigation: {
      active_navigation_item_id: "files",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "files",
        target_section: "files",
        label: "文件处理",
        group: "workspace",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.FILE_READ],
      }],
    },
  };
}

/**
 * 构造一条 exact-key FileInfo ready 响应。
 * 参数：仅允许覆盖公开 wire 字段。返回：不含正文、规则、hash 或对象键的安全测试 DTO。
 */
function fileFixture(overrides = {}) {
  return {
    file_id: `file_${"a".repeat(32)}`,
    original_name: "企业方案.docx",
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
      file_extract_id: `fext_${"b".repeat(32)}`,
      status: "ready",
      extractor_version: "docx-v1",
      text_size_bytes: 512,
      text_code_points: 300,
      estimated_tokens: 120,
      estimator_version: "file-token-estimator-v1",
      failure_error_code: null,
    },
    created_at: "2026-08-05T08:00:00Z",
    updated_at: "2026-08-05T08:00:01Z",
    completed_at: "2026-08-05T08:00:01Z",
    ...overrides,
  };
}

/** 构造列表信封；opaque cursor 只供私有状态测试。 */
function listFixture(overrides = {}) {
  return {
    files: [fileFixture()],
    next_cursor: null,
    request_id: "req_file_list_a",
    trace_id: "trc_file_list_a",
    ...overrides,
  };
}

/** 构造 fetch 可用 JSON Response。 */
function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("PRD-072 list client sends only frozen status/limit/cursor and no tenant selector", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(listFixture());
  };

  const result = await listFiles({
    controlApiUrl: "http://control.local",
    token: "session-token",
    status: "ready",
    limit: 25,
    cursor: "signed.cursor_value-1",
    tenantId: "tenant_injected",
    userId: "usr_injected",
    sortBy: "original_object_key",
  });
  assert.equal(result.files[0].original_name, "企业方案.docx");
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, "/files");
  assert.equal(url.searchParams.get("status"), "ready");
  assert.equal(url.searchParams.get("limit"), "25");
  assert.equal(url.searchParams.get("cursor"), "signed.cursor_value-1");
  assert.equal(url.searchParams.has("tenant_id"), false);
  assert.equal(url.searchParams.has("user_id"), false);
  assert.equal(url.searchParams.has("sort_by"), false);
  assert.equal(calls[0].options.headers.Authorization, "Bearer session-token");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.credentials, "omit");
});

test("PRD-072 upload emits exactly one file part without manual multipart header or retry", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      file: fileFixture({
        original_name: "brief.txt",
        file_extension: "txt",
        media_type: "text/plain",
        extract: {
          ...fileFixture().extract,
          extractor_version: "txt-v1",
        },
      }),
      request_id: "req_file_post",
      trace_id: "trc_file_post",
    }, 201);
  };
  const selected = new File(["safe upload body"], "brief.txt", { type: "text/plain" });
  const result = await uploadFile({
    controlApiUrl: "http://control.local",
    token: "session-token",
    file: selected,
    tenantId: "tenant_injected",
  });
  assert.equal(result.file.file_extension, "txt");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://control.local/files");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["Content-Type"], undefined);
  assert.deepEqual([...calls[0].options.body.keys()], ["file"]);
  assert.equal(calls[0].options.body.get("file").name, "brief.txt");
});

test("PRD-072 detail client validates the bounded file path before fetch", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return jsonResponse({
      file: fileFixture(),
      request_id: "req_file_detail",
      trace_id: "trc_file_detail",
    });
  };
  const fileId = `file_${"a".repeat(32)}`;
  const result = await getFile({
    controlApiUrl: "http://control.local",
    token: "session-token",
    fileId,
    tenantId: "tenant_injected",
  });
  assert.equal(result.file.file_id, fileId);
  assert.equal(calls[0], `http://control.local/files/${fileId}`);
  await assert.rejects(
    getFile({
      controlApiUrl: "http://control.local",
      token: "session-token",
      fileId: "file_bad/../tenant-b",
    }),
    (error) => error instanceof AuthApiError && error.code === "file.not_found",
  );
  assert.equal(calls.length, 1);
});

test("PRD-072 client rejects unsupported and oversized selections before fetch", async (t) => {
  let fetchCount = 0;
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse({});
  };
  assert.equal(isSupportedUploadName("report.pdf"), true);
  assert.equal(isSupportedUploadName("legacy.doc"), false);
  await assert.rejects(
    uploadFile({
      controlApiUrl: "http://control.local",
      token: "token",
      file: new File(["legacy"], "legacy.doc", { type: "application/msword" }),
    }),
    (error) => error instanceof AuthApiError && error.code === "file.unsupported_format",
  );
  const oversized = {
    name: "large.txt",
    size: FILE_UPLOAD_MAX_BYTES + 1,
    type: "text/plain",
    arrayBuffer: async () => new ArrayBuffer(0),
  };
  await assert.rejects(
    uploadFile({ controlApiUrl: "http://control.local", token: "token", file: oversized }),
    (error) => error instanceof AuthApiError && error.code === "file.too_large",
  );
  assert.equal(fetchCount, 0);
});

test("PRD-072 exact DTO rejects plaintext, rule, hash, object key and unknown nested fields", () => {
  const injections = [
    { ...listFixture(), extracted_text: "must-not-enter-browser" },
    { ...listFixture(), files: [{ ...fileFixture(), original_object_key: "tenant/path/blob" }] },
    { ...listFixture(), files: [{ ...fileFixture(), effective_dlp_rule_id: "dlp_private" }] },
    { ...listFixture(), files: [{ ...fileFixture(), content_fingerprint: "sha256:private" }] },
    {
      ...listFixture(),
      files: [{
        ...fileFixture(),
        extract: { ...fileFixture().extract, text: "private extract" },
      }],
    },
  ];
  for (const payload of injections) assert.equal(normalizeFileListResponse(payload), null);
  assert.equal(normalizeFileListResponse(listFixture())?.files.length, 1);
  assert.equal(normalizeFileResponse({
    file: fileFixture(),
    request_id: "req_detail",
    trace_id: "trc_detail",
  })?.file.status, "ready");
});

test("PRD-072 errors use fixed code text and never display server parser details or filename", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async () => jsonResponse({
    code: "file.parse_failed",
    message: "secret.docx: Zip parser leaked internal path C:/service/private",
    request_id: "req_file_failure",
    trace_id: "trc_file_failure",
  }, 422);
  await assert.rejects(
    listFiles({ controlApiUrl: "http://control.local", token: "token" }),
    (error) => (
      error instanceof AuthApiError
      && error.code === "file.parse_failed"
      && error.message === "文件文本解析失败"
      && error.requestId === "req_file_failure"
      && !error.message.includes("secret.docx")
    ),
  );
});

test("PRD-072 owner requires backend files navigation and keeps read/write independent", () => {
  const full = authContext();
  const fullOwner = filePageOwner(full, shellState(full));
  assert.ok(fullOwner);
  assert.equal(fullOwner.canWrite, true);
  assert.match(fileReadIdentitySnapshot(full), /file\.read/);
  assert.match(fileWriteIdentitySnapshot(full), /file\.write/);

  const readOnly = authContext({
    permissions: [PERMISSIONS.FILE_READ],
    fingerprint: "files_read_only",
  });
  const readOwner = filePageOwner(readOnly, shellState(readOnly));
  assert.ok(readOwner);
  assert.equal(readOwner.canWrite, false);
  assert.equal(filePageOwner(readOnly, shellState(readOnly), { requireWrite: true }), null);

  const writeOnly = authContext({
    permissions: [PERMISSIONS.FILE_WRITE],
    fingerprint: "files_write_only",
  });
  assert.equal(filePageOwner(writeOnly, shellState(writeOnly)), null);
  const wrongTarget = shellState(full);
  wrongTarget.navigation.navigation_items[0].target_section = "request-logs";
  assert.equal(filePageOwner(full, wrongTarget), null);
});

test("PRD-072 state hides cursor/owner and aborts late tenant operations", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = filePageOwner(context, shell);
  const state = createFilePageState();
  assert.equal(state.enter(owner), true);
  const listOperation = state.beginListOperation(owner, 7);
  const page = normalizeFileListResponse(listFixture({ next_cursor: "signed.cursor.private" }));
  assert.equal(state.acceptPage(listOperation, page, owner, 7), true);
  state.finishListOperation(listOperation);
  assert.equal(state.snapshot().canNext, true);
  assert.equal(JSON.stringify(state.snapshot()).includes("signed.cursor.private"), false);
  assert.equal(JSON.stringify(state.snapshot()).includes(context.authorization_fingerprint), false);

  const uploadOperation = state.beginUploadOperation(owner);
  assert.ok(uploadOperation);
  const changed = authContext({
    tenantId: "tenant_files_b",
    userId: "usr_files_b",
    sessionId: "ses_files_b",
    fingerprint: "files_authz_b",
  });
  const changedOwner = filePageOwner(changed, shellState(changed));
  assert.equal(state.enter(changedOwner), true);
  assert.equal(uploadOperation.controller.signal.aborted, true);
  assert.equal(state.isUploadOperationCurrent(uploadOperation, changedOwner), false);
  assert.equal(state.snapshot().files.length, 0);
});

test("PRD-072 renderer escapes filename, never exposes forbidden fields, and marks virus scanner neutral", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = filePageOwner(context, shell);
  const state = createFilePageState();
  state.enter(owner);
  const operation = state.beginListOperation(owner, 1);
  const payload = normalizeFileListResponse(listFixture({
    files: [fileFixture({ original_name: "<img src=x onerror=fileCanary()>.docx" })],
  }));
  state.acceptPage(operation, payload, owner, 1);
  const html = renderFiles(state.snapshot(), context, shell);
  assert.match(html, /&lt;img src=x onerror=fileCanary\(\)&gt;\.docx/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /tone-not-configured/);
  assert.match(html, /未配置病毒扫描/);
  assert.doesNotMatch(html, /original_object_key|extracted_object_key|effective_dlp_rule_id|content_fingerprint|extracted_text/);
  assert.doesNotMatch(html, /data-file-name=/);
});

test("PRD-072 read-only renderer creates no upload form while write owner gets one", () => {
  const full = authContext();
  const fullHtml = renderFiles(null, full, shellState(full));
  assert.match(fullHtml, /id="file-upload-form"/);
  assert.match(fullHtml, /type="file"/);

  const readOnly = authContext({
    permissions: [PERMISSIONS.FILE_READ],
    fingerprint: "files_read_only_render",
  });
  const readOnlyHtml = renderFiles(null, readOnly, shellState(readOnly));
  assert.match(readOnlyHtml, /只读文件视图/);
  assert.doesNotMatch(readOnlyHtml, /id="file-upload-form"|type="file"|data-file-upload-submit/);
});

test("PRD-072 authorized files navigation renders through the existing console shell", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = filePageOwner(context, shell);
  const state = createFilePageState();
  state.enter(owner);
  const operation = state.beginListOperation(owner, 1);
  state.acceptPage(operation, normalizeFileListResponse(listFixture()), owner, 1);
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: context,
    consoleShellState: shell,
    filesState: state.snapshot(),
  });
  assert.match(container.innerHTML, /data-active-navigation="files"/);
  assert.match(container.innerHTML, /class="panel files-panel"/);
  assert.match(container.innerHTML, /data-file-root/);
  assert.match(container.innerHTML, /企业方案\.docx/);
});

test("PRD-072 main lifecycle and responsive CSS cover cleanup, permissions, and 390px labels", async () => {
  const [mainSource, shellSource, permissionsSource, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/permissions.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /loadFilesState\(consoleShellState, refreshId\)/);
  assert.match(mainSource, /bindFilesPageInteractions\(consoleShellState\)/);
  assert.match(mainSource, /clearFileStateOnIdentityChange/);
  assert.match(mainSource, /window\.addEventListener\("pagehide"[\s\S]*filePageState\.clear\(\)/);
  assert.match(mainSource, /logoutButton[\s\S]*filePageState\.clear\(\)/);
  assert.match(mainSource, /itemId !== "files"[\s\S]*filePageState\.clear\(\)/);
  assert.match(mainSource, /filePageState\.isUploadBusy\(\) \|\| fileUploadHasSelection\(\)/);
  // DLP 阻断也是 403，但只有真实 auth.permission_denied 才能进入撤权清理分支。
  assert.match(mainSource, /error\.status === 403[\s\S]*error\.code === "auth\.permission_denied"/);
  assert.match(shellSource, /case "files":/);
  assert.match(permissionsSource, /FILE_READ: "file\.read"/);
  assert.match(permissionsSource, /FILE_WRITE: "file\.write"/);
  assert.match(css, /\.file-row\s*\{[\s\S]*grid-template-columns:/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.file-row:not\(\.file-row-head\) > \[data-label\]::before/);
  assert.match(css, /\.file-name\s*\{[\s\S]*font-size:\s*13px/);
  assert.match(css, /\.file-upload-button > span:first-child[\s\S]*width:\s*18px;[\s\S]*height:\s*18px/);
  assert.equal((css.match(/{/g) ?? []).length, (css.match(/}/g) ?? []).length);
});
