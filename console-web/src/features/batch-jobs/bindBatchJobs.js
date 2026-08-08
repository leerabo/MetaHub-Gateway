/**
 * PAGE-104 DOM bindings.
 *
 * Opaque cursors and authorization owners stay in the page state. Create-form text is read only
 * for one submit call, and assistant output is written only to the dedicated result text node.
 */

import { createBatchJobIdempotencyKey } from "../../lib/batchJobs.js";

const JOB_ID = /^bjob_[0-9a-f]{32}$/;
const TASK_ID = /^btsk_[0-9a-f]{32}$/;
const OPERATIONS = new Set(["summarize", "translate", "classify"]);
const PRIORITIES = new Set(["low", "normal", "high"]);
const STATUSES = new Set([
  "queued", "running", "completed", "partial_failed", "failed", "cancelled", "expired",
]);

export function bindBatchJobInteractions(handlers = {}, root = globalThis.document) {
  const workspace = root?.querySelector?.("[data-batch-jobs-root]");
  if (!workspace) return false;
  bindList(workspace, handlers);
  bindDetail(workspace, handlers);
  bindResult(workspace, handlers);
  bindCreate(workspace, handlers);
  return true;
}

function bindList(workspace, handlers) {
  workspace.querySelector("[data-batch-status-filter]")?.addEventListener("change", async (event) => {
    const value = String(event.currentTarget?.value ?? "");
    await handlers.onFilter?.(STATUSES.has(value) ? value : "");
  });
  workspace.querySelector("[data-batch-refresh]")?.addEventListener("click", async () => {
    await handlers.onRefresh?.();
  });
  workspace.querySelector("[data-batch-previous]")?.addEventListener("click", async () => {
    await handlers.onPreviousPage?.();
  });
  workspace.querySelector("[data-batch-next]")?.addEventListener("click", async () => {
    await handlers.onNextPage?.();
  });
  for (const button of workspace.querySelectorAll("[data-batch-select]")) {
    button.addEventListener("click", async () => {
      const batchJobId = String(button.dataset.batchSelect ?? "");
      if (JOB_ID.test(batchJobId)) await handlers.onSelect?.(batchJobId);
    });
  }
}

function bindDetail(workspace, handlers) {
  workspace.querySelector("[data-batch-task-previous]")?.addEventListener("click", async () => {
    await handlers.onTaskPreviousPage?.();
  });
  workspace.querySelector("[data-batch-task-next]")?.addEventListener("click", async () => {
    await handlers.onTaskNextPage?.();
  });
  workspace.querySelector("[data-batch-cancel]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const batchJobId = String(button?.dataset?.batchCancel ?? "");
    if (!JOB_ID.test(batchJobId)) return;
    button.disabled = true;
    try {
      const snapshot = await handlers.onCancel?.(batchJobId);
      syncBatchActionFeedback(workspace, snapshot);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  });
}

function bindResult(workspace, handlers) {
  let bindingGeneration = 0;
  for (const button of workspace.querySelectorAll("[data-batch-result]")) {
    button.addEventListener("click", async () => {
      const batchTaskId = String(button.dataset.batchResult ?? "");
      if (!TASK_ID.test(batchTaskId)) return;
      const generation = ++bindingGeneration;
      markBatchResultLoading(workspace, batchTaskId);
      try {
        const snapshot = await handlers.onResult?.(batchTaskId);
        syncBatchResultState(workspace, snapshot);
      } finally {
        const result = workspace.querySelector(".batch-result");
        if (generation === bindingGeneration && result?.isConnected) {
          result.removeAttribute("aria-busy");
        }
      }
    });
  }
  workspace.querySelector("[data-batch-result-close]")?.addEventListener("click", async () => {
    bindingGeneration += 1;
    clearBatchResultDom(workspace);
    await handlers.onCloseResult?.();
  });
}

function syncBatchResultState(workspace, snapshot) {
  const status = workspace.querySelector("[data-batch-result-status]");
  if (!status || !snapshot) return;
  if (snapshot.resultState === "error" && snapshot.resultError) {
    status.textContent = `${snapshot.resultError.code} · request_id: ${snapshot.resultError.requestId}`;
    return;
  }
  if (snapshot.resultState === "idle") status.textContent = "";
}

function bindCreate(workspace, handlers) {
  const dialog = workspace.querySelector("[data-batch-create-dialog]");
  const form = dialog?.querySelector("[data-batch-create-form]");
  const openButton = workspace.querySelector("[data-batch-create]");
  const closeButton = dialog?.querySelector("[data-batch-create-close]");
  if (!dialog || !form || !openButton) return;
  let pendingRequestFingerprint = "";
  let pendingIdempotencyKey = "";
  const open = async () => {
    if (await handlers.onOpenCreate?.() !== true) return;
    pendingRequestFingerprint = "";
    pendingIdempotencyKey = "";
    clearBatchCreateForm(form);
    dialog.hidden = false;
    syncOperationFields(form);
    syncAliasOptions(form);
    syncFileSelection(form);
    form.querySelector("input[name='name']")?.focus();
  };
  const close = async () => {
    if (await handlers.onCloseCreate?.() !== true) return;
    pendingRequestFingerprint = "";
    pendingIdempotencyKey = "";
    clearBatchCreateForm(form);
    dialog.hidden = true;
  };
  openButton.addEventListener("click", open);
  closeButton?.addEventListener("click", close);
  dialog.addEventListener("keydown", async (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    await close();
  });
  for (const radio of form.querySelectorAll("input[name='operation']")) {
    radio.addEventListener("change", () => syncOperationFields(form));
  }
  form.querySelector("select[name='environment_id']")?.addEventListener("change", () => {
    syncAliasOptions(form);
  });
  for (const checkbox of form.querySelectorAll("input[name='file_ids']")) {
    checkbox.addEventListener("change", () => syncFileSelection(form));
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
    let request;
    try {
      request = batchCreateRequestFromFormData(new FormData(form));
    } catch {
      const live = form.querySelector("[data-batch-create-live]");
      if (live) live.textContent = "请完成必填选择并检查操作参数";
      return;
    }
    lockForm(form, true);
    try {
      const fingerprint = await batchCreateRequestFingerprint(request);
      if (fingerprint !== pendingRequestFingerprint) {
        const nextKey = createBatchJobIdempotencyKey();
        pendingRequestFingerprint = fingerprint;
        pendingIdempotencyKey = nextKey;
      }
      const snapshot = await handlers.onCreate?.(request, pendingIdempotencyKey);
      syncBatchActionFeedback(workspace, snapshot);
      if (snapshot?.createState === "success") {
        pendingRequestFingerprint = "";
        pendingIdempotencyKey = "";
        clearBatchCreateForm(form);
      }
    } catch {
      const live = form.querySelector("[data-batch-create-live]");
      if (live) live.textContent = "批处理提交暂不可用";
    } finally {
      if (form.isConnected) lockForm(form, false);
    }
  });
  syncOperationFields(form);
  syncAliasOptions(form);
  syncFileSelection(form);
}

/** Build the exact Batch create body from a FormData-like object. */
export function batchCreateRequestFromFormData(data) {
  const operation = text(data?.get?.("operation"));
  const priority = text(data?.get?.("priority"));
  const fileIds = (data?.getAll?.("file_ids") ?? []).map(text).filter(Boolean);
  if (!OPERATIONS.has(operation) || !PRIORITIES.has(priority) || fileIds.length < 1) {
    throw new TypeError("batch.request_invalid");
  }
  const request = {
    name: text(data.get("name")),
    project_id: text(data.get("project_id")),
    environment_id: text(data.get("environment_id")),
    model_alias_id: text(data.get("model_alias_id")),
    operation,
    file_ids: fileIds,
    priority,
    max_duration_seconds: integerText(data.get("max_duration_seconds")),
    max_attempts: integerText(data.get("max_attempts")),
    max_output_tokens: integerText(data.get("max_output_tokens")),
  };
  if (operation === "summarize") {
    const outputLanguage = text(data.get("output_language")).trim();
    if (outputLanguage) request.output_language = outputLanguage;
  } else if (operation === "translate") {
    request.target_language = text(data.get("target_language")).trim();
  } else {
    request.classification_labels = text(data.get("classification_labels"))
      .split(/[\n,]/u)
      .map((value) => value.trim())
      .filter(Boolean);
  }
  return request;
}

/** Hash the exact create wire so an ambiguous retry can reuse its key without retaining form text. */
export async function batchCreateRequestFingerprint(request) {
  if (!request || typeof globalThis.crypto?.subtle?.digest !== "function") {
    throw new TypeError("batch.request_invalid");
  }
  const bytes = new TextEncoder().encode(JSON.stringify(request));
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", bytes));
  return [...digest].map((value) => value.toString(16).padStart(2, "0")).join("");
}

/** Clear result text synchronously before owner invalidation or AbortController cancellation. */
export function clearBatchResultDom(root = globalThis.document) {
  const bodies = root?.matches?.("[data-batch-result-body]")
    ? [root]
    : (root?.querySelectorAll?.("[data-batch-result-body]") ?? []);
  for (const body of bodies) {
    body.textContent = "";
    body.hidden = true;
    body.removeAttribute("data-batch-result-task-id");
  }
  const status = root?.querySelector?.("[data-batch-result-status]");
  if (status) status.textContent = "";
  return bodies.length > 0;
}

/** Assign assistant text once, after the main owner ticket has already accepted the response. */
export function hydrateBatchResultText(root, {
  batchJobId,
  batchTaskId,
  assistantText,
} = {}) {
  if (
    !JOB_ID.test(String(batchJobId ?? ""))
    || !TASK_ID.test(String(batchTaskId ?? ""))
    || typeof assistantText !== "string"
    || assistantText.length < 1
  ) return false;
  const workspace = root?.matches?.("[data-batch-jobs-root]")
    ? root
    : root?.querySelector?.("[data-batch-jobs-root]");
  if (!workspace || workspace.dataset.batchSelectedJob !== batchJobId) return false;
  const taskStillVisible = [...workspace.querySelectorAll("[data-batch-result]")].some(
    (button) => button.dataset.batchResult === batchTaskId,
  );
  const body = workspace.querySelector("[data-batch-result-body]");
  if (!taskStillVisible || !body) return false;
  body.textContent = "";
  body.textContent = assistantText;
  body.setAttribute("data-batch-result-task-id", batchTaskId);
  body.hidden = false;
  const status = workspace.querySelector("[data-batch-result-status]");
  if (status) status.textContent = "";
  return true;
}

function markBatchResultLoading(workspace, batchTaskId) {
  clearBatchResultDom(workspace);
  workspace.querySelector(".batch-result")?.setAttribute("aria-busy", "true");
  const status = workspace.querySelector("[data-batch-result-status]");
  if (status) status.textContent = `正在读取任务 ${batchTaskId.slice(-8)} 的结果`;
}

function syncOperationFields(form) {
  const operation = text(form.querySelector("input[name='operation']:checked")?.value);
  for (const container of form.querySelectorAll("[data-batch-operation-field]")) {
    const active = container.dataset.batchOperationField === operation;
    container.hidden = !active;
    for (const control of container.querySelectorAll("input, textarea")) {
      control.disabled = !active;
      control.required = active && operation !== "summarize";
    }
  }
}

function syncAliasOptions(form) {
  const environmentId = text(form.querySelector("select[name='environment_id']")?.value);
  const alias = form.querySelector("select[name='model_alias_id']");
  if (!alias) return;
  let first = null;
  let selectedStillValid = false;
  for (const option of alias.querySelectorAll("option[data-environment-id]")) {
    const available = option.dataset.environmentId === environmentId;
    option.hidden = !available;
    option.disabled = !available;
    if (available && !first) first = option;
    if (available && option.value === alias.value) selectedStillValid = true;
  }
  if (!selectedStillValid) alias.value = first?.value ?? "";
  alias.disabled = !first;
  syncFileSelection(form);
}

function syncFileSelection(form) {
  const selected = form.querySelectorAll("input[name='file_ids']:checked").length;
  const count = form.querySelector("[data-batch-file-count]");
  if (count) count.textContent = `已选择 ${selected} 个文件`;
  const submit = form.querySelector("button[type='submit']");
  const resourcesReady = ["project_id", "environment_id", "model_alias_id"].every(
    (name) => Boolean(text(form.querySelector(`[name='${name}']`)?.value)),
  );
  if (submit && form.getAttribute("aria-busy") !== "true") {
    submit.disabled = selected < 1 || !resourcesReady;
  }
}

function syncBatchActionFeedback(workspace, snapshot) {
  const target = workspace.querySelector("[data-batch-action-feedback]");
  if (!target || !snapshot) return;
  const error = snapshot.createError || snapshot.cancelError;
  const feedback = snapshot.createFeedback || snapshot.cancelFeedback;
  target.textContent = error
    ? `${error.code} · request_id: ${error.requestId}`
    : (feedback ? `${feedback.code} · request_id: ${feedback.requestId}` : "");
  target.classList.toggle("is-error", Boolean(error));
}

function clearBatchCreateForm(form) {
  form.reset();
  for (const control of form.querySelectorAll("input[type='text'], textarea")) control.value = "";
  const live = form.querySelector("[data-batch-create-live]");
  if (live) live.textContent = "";
  syncOperationFields(form);
  syncAliasOptions(form);
  syncFileSelection(form);
}

function lockForm(form, locked) {
  if (locked) form.setAttribute("aria-busy", "true");
  else form.removeAttribute("aria-busy");
  for (const control of form.querySelectorAll("button, input, select, textarea")) {
    control.disabled = locked;
  }
  if (!locked) {
    syncOperationFields(form);
    syncAliasOptions(form);
    syncFileSelection(form);
  }
}

function integerText(value) {
  const normalized = text(value);
  if (!/^(?:0|[1-9][0-9]*)$/.test(normalized)) throw new TypeError("batch.request_invalid");
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed)) throw new TypeError("batch.request_invalid");
  return parsed;
}

function text(value) {
  return typeof value === "string" ? value : "";
}
