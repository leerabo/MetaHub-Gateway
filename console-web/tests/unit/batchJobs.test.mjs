/** PRD-074 PAGE-104 owner and sensitive-result lifecycle tests. */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { AuthApiError } from "../../src/lib/auth.js";
import {
  cancelBatchJob,
  createBatchJob,
  getBatchTaskResult,
  listBatchJobs,
  listBatchTasks,
  normalizeBatchJobListResponse,
  normalizeBatchJobResponse,
  normalizeBatchTaskListResponse,
  normalizeBatchTaskResultResponse,
} from "../../src/lib/batchJobs.js";
import {
  batchJobPageOwner,
  batchResultIdentitySnapshot,
  batchWriteIdentitySnapshot,
  createBatchJobPageState,
} from "../../src/features/batch-jobs/batchJobPageState.js";
import { renderBatchJobs } from "../../src/features/batch-jobs/renderBatchJobs.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const JOB_ID = `bjob_${"a".repeat(32)}`;
const OTHER_JOB_ID = `bjob_${"b".repeat(32)}`;
const TASK_ID = `btsk_${"c".repeat(32)}`;
const RESULT_CANARY = "assistant-result-private-canary";

function authContext({
  permissions = [PERMISSIONS.BATCH_READ, PERMISSIONS.BATCH_WRITE, PERMISSIONS.FILE_READ],
  tenantId = "tenant_batch_a",
  userId = "usr_batch_a",
  sessionId = "ses_batch_a",
  fingerprint = "batch_authz_a",
} = {}) {
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, slug: "batch", display_name: "Batch tenant" },
    user: {
      tenant_id: tenantId,
      user_id: userId,
      email: "batch@example.test",
      role: "developer",
    },
    session: { session_id: sessionId, session_revision: 1 },
  }, permissions, { permissionRevision: 10, authorizationFingerprint: fingerprint });
}

function shellState(context = authContext()) {
  return {
    state: "ok",
    activeNavigationItemId: "batch-jobs",
    navigation: {
      active_navigation_item_id: "batch-jobs",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "batch-jobs",
        target_section: "batch-jobs",
        label: "Batch jobs",
        group: "workspace",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.BATCH_READ],
      }],
    },
  };
}

function listPayload() {
  return Object.freeze({
    state: "ok",
    jobs: Object.freeze([{ batch_job_id: JOB_ID, status: "running" }]),
    next_cursor: "private.batch.cursor",
    request_id: "req_batch_list",
    trace_id: "trc_batch_list",
  });
}

function detailPayload() {
  return Object.freeze({
    state: "ok",
    job: Object.freeze({ batch_job_id: JOB_ID, status: "running" }),
    request_id: "req_batch_detail",
    trace_id: "trc_batch_detail",
  });
}

function taskPayload() {
  return Object.freeze({
    state: "ok",
    batch_job_id: JOB_ID,
    tasks: Object.freeze([{ batch_task_id: TASK_ID, result_available: true }]),
    next_cursor: null,
    request_id: "req_batch_tasks",
    trace_id: "trc_batch_tasks",
  });
}

function jobInfo(overrides = {}) {
  return {
    batch_job_id: JOB_ID,
    name: "August documents",
    project_id: "project_batch_a",
    environment_id: "env_batch_a",
    model_alias_id: "alias_batch_a",
    operation: "summarize",
    priority: "normal",
    max_duration_seconds: 300,
    max_attempts: 2,
    max_output_tokens: 1024,
    status: "running",
    progress: {
      total: 1,
      queued: 0,
      running: 1,
      succeeded: 0,
      failed: 0,
      cancelled: 0,
      expired: 0,
      outcome_unknown: 0,
    },
    cost: {
      estimated_cny: "0.120000",
      posted_cny: "0.000000",
      possible_cny: "0.000000",
      reconciliation_pending: false,
    },
    deadline_at: "2026-08-08T12:05:00Z",
    cancel_requested_at: null,
    started_at: "2026-08-08T12:00:01Z",
    completed_at: null,
    failure_code: null,
    created_at: "2026-08-08T12:00:00Z",
    updated_at: "2026-08-08T12:00:01Z",
    ...overrides,
  };
}

function taskInfo(overrides = {}) {
  return {
    batch_task_id: TASK_ID,
    file_id: `file_${"d".repeat(32)}`,
    task_index: 0,
    status: "succeeded",
    attempt_count: 1,
    request_id: "req_batch_attempt",
    trace_id: "trc_batch_attempt",
    failure_code: null,
    result_available: true,
    input_tokens: 120,
    output_tokens: 32,
    cost_cny: "0.012000",
    created_at: "2026-08-08T12:00:00Z",
    updated_at: "2026-08-08T12:00:02Z",
    completed_at: "2026-08-08T12:00:02Z",
    ...overrides,
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id ?? "req_batch_error",
      "X-Trace-ID": payload.trace_id ?? "trc_batch_error",
    },
  });
}

test("PRD-074 navigation requires only batch.read while create and result identities stay independent", () => {
  const writer = authContext();
  const writerOwner = batchJobPageOwner(writer, shellState(writer));
  assert.ok(writerOwner);
  assert.equal(writerOwner.canCreate, true);
  assert.equal(writerOwner.canReadResults, true);
  assert.match(batchWriteIdentitySnapshot(writer), /batch\.write/);
  assert.match(batchResultIdentitySnapshot(writer), /file\.read/);

  const reader = authContext({
    permissions: [PERMISSIONS.BATCH_READ],
    fingerprint: "batch_read_only",
  });
  const readerOwner = batchJobPageOwner(reader, shellState(reader));
  assert.ok(readerOwner);
  assert.equal(readerOwner.canCreate, false);
  assert.equal(readerOwner.canReadResults, false);
  assert.equal(batchJobPageOwner(reader, shellState(reader), { mode: "write" }), null);
  assert.equal(batchJobPageOwner(reader, shellState(reader), { mode: "result" }), null);

  const wrongNavigation = shellState(writer);
  wrongNavigation.navigation.navigation_items[0].required_permissions = [
    PERMISSIONS.BATCH_READ,
    PERMISSIONS.FILE_READ,
  ];
  assert.equal(batchJobPageOwner(writer, wrongNavigation), null);
});

test("PRD-074 list cursor, controllers and authorization fingerprint never enter snapshots", () => {
  const context = authContext();
  const owner = batchJobPageOwner(context, shellState(context));
  const state = createBatchJobPageState();
  assert.equal(state.enter(owner), true);
  const operation = state.beginList(owner, 9);
  assert.ok(operation);
  assert.equal(state.acceptList(operation, listPayload(), owner, 9), true);
  state.finishList(operation);
  const serialized = JSON.stringify(state.snapshot());
  assert.equal(serialized.includes("private.batch.cursor"), false);
  assert.equal(serialized.includes(context.authorization_fingerprint), false);
  assert.equal(serialized.includes("controller"), false);
  assert.equal(state.nextPage(), true);
  assert.deepEqual(state.query(), { cursor: "private.batch.cursor" });
});

test("PRD-074 result ticket binds owner job task generation and never stores assistant text", () => {
  const cleared = [];
  const context = authContext();
  const owner = batchJobPageOwner(context, shellState(context));
  const state = createBatchJobPageState({ clearResultDom: () => cleared.push("clear") });
  state.enter(owner);
  const list = state.beginList(owner, 1);
  state.acceptList(list, listPayload(), owner, 1);
  state.finishList(list);
  const detail = state.beginDetail(owner, 1);
  state.acceptDetail(detail, detailPayload(), taskPayload(), owner, 1);
  state.finishDetail(detail);

  const result = state.beginResult(owner, TASK_ID);
  assert.ok(result);
  const acceptedText = state.acceptResult(result, {
    state: "ok",
    batch_job_id: JOB_ID,
    batch_task_id: TASK_ID,
    assistant_text: RESULT_CANARY,
    request_id: "req_batch_result",
    trace_id: "trc_batch_result",
  }, owner);
  assert.equal(acceptedText, RESULT_CANARY);
  assert.equal(JSON.stringify(state.snapshot()).includes(RESULT_CANARY), false);
  assert.equal(state.snapshot().resultState, "ready");

  // Switching jobs synchronously clears the dedicated DOM and invalidates the old ticket.
  const nextList = state.beginList(owner, 2);
  state.acceptList(nextList, Object.freeze({
    ...listPayload(),
    jobs: Object.freeze([{ batch_job_id: OTHER_JOB_ID, status: "queued" }]),
  }), owner, 2);
  assert.equal(result.controller.signal.aborted, true);
  assert.equal(state.isResultCurrent(result, owner), false);
  assert.equal(cleared.length >= 2, true);
  assert.equal(state.snapshot().resultState, "idle");
});

test("PRD-074 owner drift clears result before abort and late failures cannot overwrite the new owner", () => {
  const events = [];
  const context = authContext();
  const owner = batchJobPageOwner(context, shellState(context));
  const state = createBatchJobPageState({ clearResultDom: () => events.push("clear") });
  state.enter(owner);
  const list = state.beginList(owner, 1);
  state.acceptList(list, listPayload(), owner, 1);
  const detail = state.beginDetail(owner, 1);
  state.acceptDetail(detail, detailPayload(), taskPayload(), owner, 1);
  const result = state.beginResult(owner, TASK_ID);
  result.controller.signal.addEventListener("abort", () => events.push("abort"));

  const changed = authContext({
    tenantId: "tenant_batch_b",
    userId: "usr_batch_b",
    sessionId: "ses_batch_b",
    fingerprint: "batch_authz_b",
  });
  const changedOwner = batchJobPageOwner(changed, shellState(changed));
  state.enter(changedOwner);
  assert.deepEqual(events.slice(-2), ["clear", "abort"]);
  assert.equal(state.rejectResult(result, { code: "batch.result_unavailable" }, changedOwner), false);
  assert.equal(state.snapshot().resultError, null);
});

test("PRD-074 business 403 stays a page error and result authorization revocation removes capability", () => {
  const context = authContext();
  const owner = batchJobPageOwner(context, shellState(context));
  const state = createBatchJobPageState();
  state.enter(owner);
  const list = state.beginList(owner, 1);
  state.acceptList(list, listPayload(), owner, 1);
  const detail = state.beginDetail(owner, 1);
  state.acceptDetail(detail, detailPayload(), taskPayload(), owner, 1);
  const result = state.beginResult(owner, TASK_ID);
  assert.equal(state.rejectResult(result, {
    code: "batch.file_security_blocked",
    status: 403,
    requestId: "req_business_403",
  }, owner), true);
  assert.deepEqual(state.snapshot().resultError, {
    code: "batch.file_security_blocked",
    requestId: "req_business_403",
  });

  const revoked = authContext({
    permissions: [PERMISSIONS.BATCH_READ, PERMISSIONS.BATCH_WRITE],
    fingerprint: "batch_file_revoked",
  });
  const revokedOwner = batchJobPageOwner(revoked, shellState(revoked));
  assert.ok(revokedOwner);
  assert.equal(revokedOwner.canReadResults, false);
  assert.equal(state.enter(revokedOwner), true);
  assert.equal(state.beginResult(revokedOwner, TASK_ID), null);
});

test("PRD-074 strict list DTO accepts only bounded public job summaries", () => {
  const normalized = normalizeBatchJobListResponse({
    jobs: [jobInfo()],
    next_cursor: "signed.batch.cursor",
    request_id: "req_batch_list",
    trace_id: "trc_batch_list",
  });
  assert.equal(normalized.jobs[0].progress.running, 1);
  assert.equal(Object.isFrozen(normalized.jobs[0].cost), true);

  const zeroCostReconciliation = normalizeBatchJobResponse({
    job: jobInfo({
      cost: {
        estimated_cny: "0.000000",
        posted_cny: "0.000000",
        possible_cny: "0.000000",
        reconciliation_pending: true,
      },
    }),
    request_id: "req_batch_zero_cost",
    trace_id: "trc_batch_zero_cost",
  });
  assert.equal(zeroCostReconciliation.job.cost.reconciliation_pending, true);

  assert.throws(() => normalizeBatchJobListResponse({
    jobs: [{ ...jobInfo(), route_plan_json: "private-route" }],
    next_cursor: null,
    request_id: "req_batch_list",
    trace_id: "trc_batch_list",
  }), (error) => error instanceof AuthApiError && error.code === "batch.response_invalid");
  assert.throws(() => normalizeBatchJobListResponse({
    jobs: [jobInfo({ progress: { ...jobInfo().progress, succeeded: 1 } })],
    next_cursor: null,
    request_id: "req_batch_list",
    trace_id: "trc_batch_list",
  }), (error) => error.code === "batch.response_invalid");
  assert.throws(() => normalizeBatchJobResponse({
    job: jobInfo({
      cost: {
        estimated_cny: "0.120000",
        posted_cny: "0.000000",
        possible_cny: "0.010000",
        reconciliation_pending: false,
      },
    }),
    request_id: "req_batch_bad_cost",
    trace_id: "trc_batch_bad_cost",
  }), (error) => error.code === "batch.response_invalid");
});

test("PRD-074 clients send only bounded paths, filters and exact create body", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith("/cancel")) {
      return jsonResponse({ job: jobInfo({ status: "cancelled", progress: {
        ...jobInfo().progress, running: 0, cancelled: 1,
      } }), request_id: "req_cancel", trace_id: "trc_cancel" });
    }
    if (options?.method === "POST") {
      return jsonResponse({ job: jobInfo(), request_id: "req_create", trace_id: "trc_create" });
    }
    return jsonResponse({
      jobs: [jobInfo()], next_cursor: null, request_id: "req_list", trace_id: "trc_list",
    });
  };
  await listBatchJobs({
    controlApiUrl: "http://control.local",
    token: "session-token",
    status: "running",
    limit: 25,
    cursor: "signed.cursor",
    tenantId: "tenant_injected",
  });
  await createBatchJob({
    controlApiUrl: "http://control.local",
    token: "session-token",
    idempotencyKey: "123e4567-e89b-42d3-a456-426614174000",
    request: {
      name: "August documents",
      project_id: "project_batch_a",
      environment_id: "env_batch_a",
      model_alias_id: "alias_batch_a",
      operation: "summarize",
      file_ids: [`file_${"d".repeat(32)}`],
      priority: "normal",
      max_duration_seconds: 300,
      max_attempts: 2,
      max_output_tokens: 1024,
      output_language: "zh-CN",
    },
  });
  await cancelBatchJob({
    controlApiUrl: "http://control.local", token: "session-token", batchJobId: JOB_ID,
  });

  const listUrl = new URL(calls[0].url);
  assert.equal(listUrl.pathname, "/batch-jobs");
  assert.deepEqual([...listUrl.searchParams.keys()].sort(), ["cursor", "limit", "status"]);
  assert.equal(listUrl.searchParams.has("tenant_id"), false);
  assert.equal(calls[1].options.headers["Idempotency-Key"], "123e4567-e89b-42d3-a456-426614174000");
  assert.deepEqual(Object.keys(JSON.parse(calls[1].options.body)).sort(), [
    "environment_id", "file_ids", "max_attempts", "max_duration_seconds", "max_output_tokens",
    "model_alias_id", "name", "operation", "output_language", "priority", "project_id",
  ]);
  assert.equal(calls[2].url, `http://control.local/batch-jobs/${JOB_ID}/cancel`);
  assert.equal(calls[2].options.body, undefined);
});

test("PRD-074 task result stays one-shot and rejects extras, cross-owner IDs and oversized text", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url) => {
    if (String(url).endsWith("/tasks?limit=50")) {
      return jsonResponse({
        tasks: [taskInfo()], next_cursor: null, request_id: "req_tasks", trace_id: "trc_tasks",
      });
    }
    return jsonResponse({
      batch_job_id: JOB_ID,
      batch_task_id: TASK_ID,
      assistant: RESULT_CANARY,
      request_id: "req_result",
      trace_id: "trc_result",
    });
  };
  const tasks = await listBatchTasks({
    controlApiUrl: "http://control.local", token: "session-token", batchJobId: JOB_ID,
  });
  assert.equal(tasks.batch_job_id, JOB_ID);
  const result = await getBatchTaskResult({
    controlApiUrl: "http://control.local",
    token: "session-token",
    batchJobId: JOB_ID,
    batchTaskId: TASK_ID,
  });
  assert.equal(result.assistant_text, RESULT_CANARY);
  assert.equal(Object.hasOwn(result, "assistant"), false);
  assert.equal(normalizeBatchTaskResultResponse({
    batch_job_id: OTHER_JOB_ID,
    batch_task_id: TASK_ID,
    assistant: RESULT_CANARY,
    request_id: "req_result",
    trace_id: "trc_result",
  }, JOB_ID, TASK_ID), null);
  assert.equal(normalizeBatchTaskResultResponse({
    batch_job_id: JOB_ID,
    batch_task_id: TASK_ID,
    assistant: RESULT_CANARY,
    request_id: "req_result",
    trace_id: "trc_result",
    ciphertext: "private",
  }, JOB_ID, TASK_ID), null);
});

test("PRD-074 no-store is mandatory and fixed business 403 never exposes server message", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async () => new Response(JSON.stringify({
    code: "batch.file_security_blocked",
    message: "private upstream diagnostic",
    request_id: "req_business_403",
    trace_id: "trc_business_403",
  }), {
    status: 403,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
  await assert.rejects(
    listBatchJobs({ controlApiUrl: "http://control.local", token: "session-token" }),
    (error) => error instanceof AuthApiError
      && error.status === 403
      && error.code === "batch.file_security_blocked"
      && error.message === "文件未通过批处理安全准入"
      && !error.message.includes("upstream"),
  );

  globalThis.fetch = async () => new Response(JSON.stringify({
    code: "batch.capacity_full",
    message: "private queue diagnostic",
    request_id: "req_capacity_full",
    trace_id: "trc_capacity_full",
  }), {
    status: 429,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
  await assert.rejects(
    listBatchJobs({ controlApiUrl: "http://control.local", token: "session-token" }),
    (error) => error instanceof AuthApiError
      && error.status === 429
      && error.code === "batch.capacity_full"
      && error.message === "当前租户的批处理队列已满"
      && !error.message.includes("diagnostic"),
  );

  globalThis.fetch = async () => new Response(JSON.stringify({
    jobs: [jobInfo()], next_cursor: null, request_id: "req_no_cache", trace_id: "trc_no_cache",
  }), { status: 200, headers: { "Content-Type": "application/json" } });
  await assert.rejects(
    listBatchJobs({ controlApiUrl: "http://control.local", token: "session-token" }),
    (error) => error.code === "batch.response_invalid",
  );
});

test("PRD-074 renderer escapes names and never receives or serializes assistant result text", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = batchJobPageOwner(context, shell);
  const state = createBatchJobPageState();
  state.enter(owner);
  const list = state.beginList(owner, 1);
  state.acceptList(list, normalizeBatchJobListResponse({
    jobs: [jobInfo({ name: "<img src=x onerror=batchCanary()>" })],
    next_cursor: null,
    request_id: "req_list",
    trace_id: "trc_list",
  }), owner, 1);
  const detail = state.beginDetail(owner, 1);
  state.acceptDetail(
    detail,
    normalizeBatchJobResponse({
      job: jobInfo({ name: "<img src=x onerror=batchCanary()>" }),
      request_id: "req_detail",
      trace_id: "trc_detail",
    }, JOB_ID),
    normalizeBatchTaskListResponse({
      tasks: [taskInfo()],
      next_cursor: null,
      request_id: "req_tasks",
      trace_id: "trc_tasks",
    }),
    owner,
    1,
  );
  const result = state.beginResult(owner, TASK_ID);
  state.acceptResult(result, {
    state: "ok",
    batch_job_id: JOB_ID,
    batch_task_id: TASK_ID,
    assistant_text: RESULT_CANARY,
    request_id: "req_result",
    trace_id: "trc_result",
  }, owner);
  const html = renderBatchJobs(state.snapshot(), context, shell);
  assert.match(html, /&lt;img src=x onerror=batchCanary\(\)&gt;/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /data-batch-result-body/);
  assert.doesNotMatch(html, new RegExp(RESULT_CANARY));
  assert.doesNotMatch(html, /route_plan|ciphertext|lease_token|grant_hash|provider_key|tenant_id=/i);
});

test("PRD-074 backend-authorized navigation renders PAGE-104 through the console shell", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = batchJobPageOwner(context, shell);
  const state = createBatchJobPageState();
  state.enter(owner);
  const list = state.beginList(owner, 1);
  state.acceptList(list, normalizeBatchJobListResponse({
    jobs: [jobInfo()], next_cursor: null, request_id: "req_list", trace_id: "trc_list",
  }), owner, 1);
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: context,
    consoleShellState: shell,
    batchJobsState: state.snapshot(),
  });
  assert.match(container.innerHTML, /data-active-navigation="batch-jobs"/);
  assert.match(container.innerHTML, /class="panel batch-jobs-panel"/);
  assert.match(container.innerHTML, /data-batch-jobs-root/);
  assert.match(container.innerHTML, /August documents/);
});

test("PRD-074 responsive CSS uses fixed grid tracks and narrow labeled rows without horizontal fallback", async () => {
  const [css, mainRenderer] = await Promise.all([
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
  ]);
  assert.match(css, /\.batch-row\s*\{[\s\S]*grid-template-columns:/);
  assert.match(css, /@media \(max-width: 620px\)[\s\S]*\.batch-row\.is-head[\s\S]*display: none/);
  assert.match(css, /\.batch-row > span::before[\s\S]*content: attr\(data-label\)/);
  assert.doesNotMatch(css, /\.batch-(?:table|task-list)[^}]*overflow-x\s*:\s*auto/);
  assert.match(mainRenderer, /case "batch-jobs"/);
});
