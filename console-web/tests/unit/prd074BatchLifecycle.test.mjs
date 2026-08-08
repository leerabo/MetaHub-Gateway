/** PAGE-104 interaction and main-application lifecycle coverage. */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  batchCreateRequestFingerprint,
  batchCreateRequestFromFormData,
  clearBatchResultDom,
  hydrateBatchResultText,
} from "../../src/features/batch-jobs/bindBatchJobs.js";
import {
  batchJobPageOwner,
  createBatchJobPageState,
} from "../../src/features/batch-jobs/batchJobPageState.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const JOB_ID = `bjob_${"a".repeat(32)}`;
const TASK_ID = `btsk_${"b".repeat(32)}`;
const FILE_ID = `file_${"c".repeat(32)}`;

function context(permissions, fingerprint = "batch_lifecycle_a") {
  return withPermissionSnapshot({
    tenant: { tenant_id: "tenant_batch_a" },
    user: { tenant_id: "tenant_batch_a", user_id: "usr_batch_a", role: "developer" },
    session: { session_id: "ses_batch_a", session_revision: 1 },
  }, permissions, { permissionRevision: 10, authorizationFingerprint: fingerprint });
}

function shell(authContext) {
  return {
    state: "ok",
    activeNavigationItemId: "batch-jobs",
    navigation: {
      active_navigation_item_id: "batch-jobs",
      permission_codes: authContext.permission_codes,
      permission_revision: authContext.permission_revision,
      authorization_fingerprint: authContext.authorization_fingerprint,
      navigation_items: [{
        id: "batch-jobs",
        target_section: "batch-jobs",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.BATCH_READ],
      }],
    },
  };
}

function listPayload(nextCursor = null) {
  return {
    state: "ok",
    jobs: [{ batch_job_id: JOB_ID, status: "running" }],
    next_cursor: nextCursor,
  };
}

function taskPayload(nextCursor = null) {
  return {
    state: "ok",
    batch_job_id: JOB_ID,
    tasks: [{ batch_task_id: TASK_ID, result_available: true }],
    next_cursor: nextCursor,
  };
}

function formData(values) {
  return {
    get(name) {
      const value = values[name];
      return Array.isArray(value) ? value[0] : value;
    },
    getAll(name) {
      const value = values[name];
      return Array.isArray(value) ? value : (value === undefined ? [] : [value]);
    },
  };
}

test("PRD-074 cancel owner does not inherit the file.read requirement from create", () => {
  const authContext = context([PERMISSIONS.BATCH_READ, PERMISSIONS.BATCH_WRITE]);
  const owner = batchJobPageOwner(authContext, shell(authContext));
  assert.ok(owner);
  assert.equal(owner.canCancel, true);
  assert.equal(owner.canCreate, false);
  assert.ok(batchJobPageOwner(authContext, shell(authContext), { mode: "write" }));
  assert.equal(batchJobPageOwner(authContext, shell(authContext), { mode: "create" }), null);

  const state = createBatchJobPageState();
  state.enter(owner);
  const list = state.beginList(owner, 1);
  state.acceptList(list, listPayload(), owner, 1);
  state.finishList(list);
  const detail = state.beginDetail(owner, 1);
  state.acceptDetail(
    detail,
    { state: "ok", job: { batch_job_id: JOB_ID, status: "running" } },
    taskPayload(),
    owner,
    1,
  );
  state.finishDetail(detail);
  assert.ok(state.beginCancel(owner, JOB_ID));
});

test("PRD-074 create candidates are a tenant-free bounded projection", () => {
  const authContext = context([
    PERMISSIONS.BATCH_READ,
    PERMISSIONS.BATCH_WRITE,
    PERMISSIONS.FILE_READ,
  ]);
  const owner = batchJobPageOwner(authContext, shell(authContext));
  const state = createBatchJobPageState();
  state.enter(owner);
  const list = state.beginList(owner, 2);
  assert.equal(state.acceptList(list, listPayload(), owner, 2, {
    projects: [{
      project_id: "project_batch_a",
      name: "Finance",
      status: "active",
      tenant_id: "tenant_private_canary",
    }],
    environments: [{
      environment_id: "env_batch_a",
      code: "prod",
      name: "Production",
      status: "active",
      tenant_id: "tenant_private_canary",
    }],
    modelAliases: [{
      model_alias_id: "alias_batch_a",
      alias: "document-default",
      environment: "prod",
      status: "active",
      tenant_id: "tenant_private_canary",
      provider_key: "provider-private-canary",
    }],
    files: [{
      file_id: FILE_ID,
      original_name: "quarterly.txt",
      status: "ready",
      extract: { status: "ready", plaintext: "document-private-canary" },
      tenant_id: "tenant_private_canary",
    }],
  }), true);
  const serialized = JSON.stringify(state.snapshot());
  assert.doesNotMatch(serialized, /tenant_private_canary|provider-private-canary|document-private-canary/);
  assert.deepEqual(state.snapshot().createOptions.modelAliases[0], {
    model_alias_id: "alias_batch_a",
    environment_id: "env_batch_a",
    label: "document-default",
  });
  assert.equal(state.snapshot().createOptionsState, "ready");
});

test("PRD-074 task paging keeps opaque cursors private and clears result DOM first", () => {
  const events = [];
  const authContext = context([
    PERMISSIONS.BATCH_READ,
    PERMISSIONS.BATCH_WRITE,
    PERMISSIONS.FILE_READ,
  ]);
  const owner = batchJobPageOwner(authContext, shell(authContext));
  const state = createBatchJobPageState({ clearResultDom: () => events.push("clear") });
  state.enter(owner);
  const list = state.beginList(owner, 3);
  state.acceptList(list, listPayload(), owner, 3);
  state.finishList(list);
  const detail = state.beginDetail(owner, 3);
  state.acceptDetail(
    detail,
    { state: "ok", job: { batch_job_id: JOB_ID, status: "running" } },
    taskPayload("private.task.cursor"),
    owner,
    3,
  );
  state.finishDetail(detail);
  const result = state.beginResult(owner, TASK_ID);
  assert.ok(result);
  assert.equal(state.nextTaskPage(), true);
  assert.deepEqual(state.taskQuery(), { cursor: "private.task.cursor" });
  assert.equal(result.controller.signal.aborted, true);
  assert.equal(events.length > 0, true);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /private\.task\.cursor/);
});

test("PRD-074 conditional create form produces an exact operation body and private retry hash", async () => {
  const request = batchCreateRequestFromFormData(formData({
    name: "Translate reports",
    project_id: "project_batch_a",
    environment_id: "env_batch_a",
    model_alias_id: "alias_batch_a",
    operation: "translate",
    file_ids: [FILE_ID],
    priority: "high",
    max_duration_seconds: "600",
    max_attempts: "3",
    max_output_tokens: "2048",
    target_language: "en-US",
    output_language: "must-not-leak",
    classification_labels: "must-not-leak,also-not",
    tenant_id: "must-not-leak",
  }));
  assert.deepEqual(Object.keys(request).sort(), [
    "environment_id", "file_ids", "max_attempts", "max_duration_seconds", "max_output_tokens",
    "model_alias_id", "name", "operation", "priority", "project_id", "target_language",
  ]);
  assert.equal(request.target_language, "en-US");
  assert.equal(JSON.stringify(request).includes("must-not-leak"), false);

  const classify = batchCreateRequestFromFormData(formData({
    name: "Classify reports",
    project_id: "project_batch_a",
    environment_id: "env_batch_a",
    model_alias_id: "alias_batch_a",
    operation: "classify",
    file_ids: [FILE_ID],
    priority: "normal",
    max_duration_seconds: "300",
    max_attempts: "2",
    max_output_tokens: "1024",
    classification_labels: "invoice\ncontract, policy ",
  }));
  assert.deepEqual(classify.classification_labels, ["invoice", "contract", "policy"]);
  assert.equal(Object.hasOwn(classify, "target_language"), false);
  const fingerprint = await batchCreateRequestFingerprint(request);
  assert.match(fingerprint, /^[0-9a-f]{64}$/);
  assert.equal(fingerprint.includes("Translate"), false);
  assert.notEqual(fingerprint, await batchCreateRequestFingerprint(classify));
});

test("PRD-074 assistant output is one-shot textContent and is synchronously erasable", () => {
  const body = {
    textContent: "",
    hidden: true,
    attributes: new Map(),
    setAttribute(name, value) { this.attributes.set(name, value); },
    removeAttribute(name) { this.attributes.delete(name); },
  };
  const status = { textContent: "loading" };
  const workspace = {
    dataset: { batchSelectedJob: JOB_ID },
    matches: (selector) => selector === "[data-batch-jobs-root]",
    querySelector(selector) {
      if (selector === "[data-batch-result-body]") return body;
      if (selector === "[data-batch-result-status]") return status;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-batch-result]") return [{ dataset: { batchResult: TASK_ID } }];
      if (selector === "[data-batch-result-body]") return [body];
      return [];
    },
  };
  const canary = "<img src=x onerror=privateCanary()>";
  assert.equal(hydrateBatchResultText(workspace, {
    batchJobId: JOB_ID,
    batchTaskId: TASK_ID,
    assistantText: canary,
  }), true);
  assert.equal(body.textContent, canary);
  assert.equal(body.hidden, false);
  assert.equal(clearBatchResultDom(workspace), true);
  assert.equal(body.textContent, "");
  assert.equal(body.hidden, true);
  assert.equal(status.textContent, "");
});

test("PRD-074 main application wires loader, bind, identity cleanup and result hydration", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(source, /const batchJobsState = await loadBatchJobsState\(consoleShellState, refreshId/);
  assert.match(source, /batchJobsState,/);
  assert.match(source, /bindBatchJobsPageInteractions\(consoleShellState\)/);
  assert.match(source, /clearBatchJobStateOnIdentityChange\(authSession\.context, context\)/);
  assert.match(source, /hydrateBatchResultText\(globalThis\.document/);
  assert.match(source, /if \(consoleShellState\?\.activeNavigationItemId !== "batch-jobs"\)/);
  assert.doesNotMatch(source, /tenant_id[^\n]*listBatchJobs|listBatchJobs[^\n]*tenant_id/);
});
