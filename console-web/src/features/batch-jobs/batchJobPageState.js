/**
 * PAGE-104 owner and lifecycle state.
 *
 * Assistant text and create-form values never enter this state or its snapshots. A successful
 * result is returned once to the caller for a dedicated textContent assignment.
 */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasEveryPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

const NAVIGATION_ID = "batch-jobs";
const JOB_ID = /^bjob_[0-9a-f]{32}$/;
const TASK_ID = /^btsk_[0-9a-f]{32}$/;
const FILE_ID = /^file_[0-9a-f]{32}$/;
const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const STATUS_VALUES = new Set([
  "queued", "running", "completed", "partial_failed", "failed", "cancelled", "expired",
]);

export function batchReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.BATCH_READ);
}

/** Cancellation requires batch.read + batch.write, but does not require file.read. */
export function batchWriteIdentitySnapshot(authContext) {
  if (!hasEveryPermission(authContext, [PERMISSIONS.BATCH_READ, PERMISSIONS.BATCH_WRITE])) return "";
  return JSON.stringify([
    authorizationIdentitySnapshot(authContext, PERMISSIONS.BATCH_READ),
    authorizationIdentitySnapshot(authContext, PERMISSIONS.BATCH_WRITE),
  ]);
}

/** Creation additionally requires file.read because the user must choose approved tenant files. */
export function batchCreateIdentitySnapshot(authContext) {
  if (!hasEveryPermission(
    authContext,
    [PERMISSIONS.BATCH_READ, PERMISSIONS.BATCH_WRITE, PERMISSIONS.FILE_READ],
  )) return "";
  return JSON.stringify([
    batchWriteIdentitySnapshot(authContext),
    authorizationIdentitySnapshot(authContext, PERMISSIONS.FILE_READ),
  ]);
}

export function batchResultIdentitySnapshot(authContext) {
  if (!hasEveryPermission(authContext, [PERMISSIONS.BATCH_READ, PERMISSIONS.FILE_READ])) return "";
  return JSON.stringify([
    authorizationIdentitySnapshot(authContext, PERMISSIONS.BATCH_READ),
    authorizationIdentitySnapshot(authContext, PERMISSIONS.FILE_READ),
  ]);
}

export function batchJobPageOwner(authContext, consoleShellState, { mode = "read" } = {}) {
  const readIdentity = batchReadIdentitySnapshot(authContext);
  const writeIdentity = batchWriteIdentitySnapshot(authContext);
  const createIdentity = batchCreateIdentitySnapshot(authContext);
  const resultIdentity = batchResultIdentitySnapshot(authContext);
  const tenantId = mirroredTenantId(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === NAVIGATION_ID)
    : null;
  const requiredPermissions = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  const authPermissions = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  if (
    !readIdentity
    || !tenantId
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || item?.target_section !== NAVIGATION_ID
    || item?.visible === false
    || item?.disabled === true
    || JSON.stringify(requiredPermissions) !== JSON.stringify([PERMISSIONS.BATCH_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(authPermissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
    || (mode === "write" && !writeIdentity)
    || (mode === "create" && !createIdentity)
    || (mode === "result" && !resultIdentity)
    || !["read", "write", "create", "result"].includes(mode)
  ) return null;
  return Object.freeze({
    readIdentity,
    writeIdentity,
    createIdentity,
    resultIdentity,
    tenantId,
    navigation: NAVIGATION_ID,
    navigationOwner: JSON.stringify([
      item.id,
      item.target_section,
      requiredPermissions,
      revision,
      fingerprint,
    ]),
    canCreate: Boolean(createIdentity),
    canCancel: Boolean(writeIdentity),
    canReadResults: Boolean(resultIdentity),
  });
}

export function createBatchJobPageState({ clearResultDom = () => {} } = {}) {
  let ownerKey = "";
  let ownerCanCreate = false;
  let ownerCanCancel = false;
  let ownerCanReadResults = false;
  let jobs = Object.freeze([]);
  let selectedJobId = "";
  let job = null;
  let tasks = Object.freeze([]);
  let status = "";
  let cursor = null;
  let cursorStack = [];
  let nextCursor = null;
  let taskCursor = null;
  let taskCursorStack = [];
  let taskNextCursor = null;
  let listState = "loading";
  let listError = null;
  let detailState = "idle";
  let detailError = null;
  let resultState = "idle";
  let resultError = null;
  let resultTaskId = "";
  let resultRequestId = "";
  let createOpen = false;
  let createOptionsState = "idle";
  let createOptionsError = null;
  let createOptions = emptyCreateOptions();
  let createState = "idle";
  let createError = null;
  let createFeedback = null;
  let cancelState = "idle";
  let cancelError = null;
  let cancelFeedback = null;
  let listGeneration = 0;
  let listOperation = null;
  let detailGeneration = 0;
  let detailOperation = null;
  let resultGeneration = 0;
  let resultOperation = null;
  let createGeneration = 0;
  let createOperation = null;
  let cancelGeneration = 0;
  let cancelOperation = null;

  function enter(owner) {
    const nextKey = keyFromOwner(owner);
    if (!nextKey) {
      clear();
      return false;
    }
    if (
      nextKey !== ownerKey
      || ownerCanCreate !== owner.canCreate
      || ownerCanCancel !== owner.canCancel
      || ownerCanReadResults !== owner.canReadResults
    ) {
      resetAll();
      ownerKey = nextKey;
      ownerCanCreate = owner.canCreate === true;
      ownerCanCancel = owner.canCancel === true;
      ownerCanReadResults = owner.canReadResults === true;
    }
    return true;
  }

  function clear() {
    resetAll();
    ownerKey = "";
    ownerCanCreate = false;
    ownerCanCancel = false;
    ownerCanReadResults = false;
  }

  function query() {
    return Object.freeze({ cursor });
  }

  function listQuery() {
    return Object.freeze({ status, cursor });
  }

  function taskQuery() {
    return Object.freeze({ cursor: taskCursor });
  }

  function setStatus(value) {
    if (listOperation || createOperation || cancelOperation) return false;
    const normalized = STATUS_VALUES.has(String(value ?? "")) ? String(value) : "";
    if (normalized === status && cursor === null && cursorStack.length === 0) return false;
    status = normalized;
    resetListPage();
    return true;
  }

  function beginList(owner, refreshGeneration) {
    const key = keyFromOwner(owner);
    if (!validOperationOwner(key, refreshGeneration)) return null;
    abortOperation(listOperation);
    // Any shell redraw destroys the one-shot result node, so clear it synchronously before GET.
    clearResult();
    listGeneration += 1;
    listOperation = operationTicket(key, listGeneration, refreshGeneration);
    listState = "loading";
    listError = null;
    return listOperation;
  }

  function isListCurrent(operation, owner, refreshGeneration) {
    return isCurrent(
      operation,
      listOperation,
      listGeneration,
      keyFromOwner(owner),
      refreshGeneration,
    );
  }

  function acceptList(operation, payload, owner, refreshGeneration, optionPayload = undefined) {
    if (
      !isListCurrent(operation, owner, refreshGeneration)
      || payload?.state !== "ok"
      || !Array.isArray(payload.jobs)
      || payload.jobs.length > 100
      || payload.jobs.some((candidate) => !JOB_ID.test(String(candidate?.batch_job_id ?? "")))
    ) return false;
    jobs = Object.freeze([...payload.jobs]);
    nextCursor = payload.next_cursor ?? null;
    if (!jobs.some((candidate) => candidate.batch_job_id === selectedJobId)) {
      selectJobInternal(jobs[0]?.batch_job_id ?? "");
    }
    if (optionPayload !== undefined) acceptCreateOptionsInternal(optionPayload);
    listState = "ready";
    listError = null;
    return true;
  }

  function rejectList(operation, error, owner, refreshGeneration) {
    if (!isListCurrent(operation, owner, refreshGeneration)) return false;
    jobs = Object.freeze([]);
    selectJobInternal("");
    listState = "error";
    listError = fixedError(error, "batch.query_unavailable");
    return true;
  }

  function finishList(operation) {
    if (listOperation === operation) listOperation = null;
  }

  function selectJob(batchJobId) {
    const normalized = String(batchJobId ?? "");
    if (!JOB_ID.test(normalized) || !jobs.some((candidate) => candidate.batch_job_id === normalized)) {
      return false;
    }
    if (normalized !== selectedJobId) selectJobInternal(normalized);
    return true;
  }

  function beginDetail(owner, refreshGeneration) {
    const key = keyFromOwner(owner);
    if (!validOperationOwner(key, refreshGeneration) || !JOB_ID.test(selectedJobId)) return null;
    abortOperation(detailOperation);
    clearResult();
    detailGeneration += 1;
    detailOperation = Object.freeze({
      ...operationTicket(key, detailGeneration, refreshGeneration),
      batchJobId: selectedJobId,
      taskCursor,
    });
    detailState = "loading";
    detailError = null;
    return detailOperation;
  }

  function isDetailCurrent(operation, owner, refreshGeneration) {
    return isCurrent(
      operation,
      detailOperation,
      detailGeneration,
      keyFromOwner(owner),
      refreshGeneration,
    ) && operation.batchJobId === selectedJobId && operation.taskCursor === taskCursor;
  }

  function acceptDetail(operation, detailPayload, taskPayload, owner, refreshGeneration) {
    if (
      !isDetailCurrent(operation, owner, refreshGeneration)
      || detailPayload?.state !== "ok"
      || detailPayload.job?.batch_job_id !== selectedJobId
      || taskPayload?.state !== "ok"
      || (taskPayload.batch_job_id !== undefined && taskPayload.batch_job_id !== selectedJobId)
      || !Array.isArray(taskPayload.tasks)
      || taskPayload.tasks.length > 100
      || taskPayload.tasks.some((task) => !TASK_ID.test(String(task?.batch_task_id ?? "")))
    ) return false;
    job = detailPayload.job;
    tasks = Object.freeze([...taskPayload.tasks]);
    taskNextCursor = taskPayload.next_cursor ?? null;
    detailState = "ready";
    detailError = null;
    return true;
  }

  function rejectDetail(operation, error, owner, refreshGeneration) {
    if (!isDetailCurrent(operation, owner, refreshGeneration)) return false;
    job = null;
    tasks = Object.freeze([]);
    taskNextCursor = null;
    clearResult();
    detailState = "error";
    detailError = fixedError(error, "batch.query_unavailable");
    return true;
  }

  function finishDetail(operation) {
    if (detailOperation === operation) detailOperation = null;
  }

  function beginResult(owner, batchTaskId) {
    const key = keyFromOwner(owner);
    const normalizedTaskId = String(batchTaskId ?? "");
    if (
      !key
      || key !== ownerKey
      || !ownerCanReadResults
      || owner?.canReadResults !== true
      || !JOB_ID.test(selectedJobId)
      || !TASK_ID.test(normalizedTaskId)
      || !tasks.some((task) => task.batch_task_id === normalizedTaskId && task.result_available === true)
    ) return null;
    clearResult();
    resultGeneration += 1;
    resultOperation = Object.freeze({
      ownerKey: key,
      generation: resultGeneration,
      batchJobId: selectedJobId,
      batchTaskId: normalizedTaskId,
      controller: new AbortController(),
    });
    resultTaskId = normalizedTaskId;
    resultState = "loading";
    return resultOperation;
  }

  function isResultCurrent(operation, owner) {
    return Boolean(
      operation
      && operation === resultOperation
      && !operation.controller.signal.aborted
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.generation === resultGeneration
      && operation.batchJobId === selectedJobId
      && operation.batchTaskId === resultTaskId
      && ownerCanReadResults
      && owner?.canReadResults === true
    );
  }

  function acceptResult(operation, payload, owner) {
    if (
      !isResultCurrent(operation, owner)
      || payload?.state !== "ok"
      || payload.batch_job_id !== operation.batchJobId
      || payload.batch_task_id !== operation.batchTaskId
      || typeof payload.assistant_text !== "string"
      || payload.assistant_text.length < 1
      || typeof payload.request_id !== "string"
    ) return null;
    resultState = "ready";
    resultError = null;
    resultRequestId = payload.request_id;
    return payload.assistant_text;
  }

  function rejectResult(operation, error, owner) {
    if (!isResultCurrent(operation, owner)) return false;
    clearResultDomAndMetadata();
    resultState = "error";
    resultError = fixedError(error, "batch.result_unavailable");
    resultTaskId = operation.batchTaskId;
    return true;
  }

  function finishResult(operation) {
    if (resultOperation === operation) resultOperation = null;
  }

  function invalidateResult() {
    clearResult();
  }

  function nextPage() {
    if (!nextCursor || listOperation || createOperation || cancelOperation) return false;
    cursorStack = [...cursorStack, cursor];
    cursor = nextCursor;
    nextCursor = null;
    jobs = Object.freeze([]);
    selectJobInternal("");
    listState = "loading";
    return true;
  }

  function previousPage() {
    if (!cursorStack.length || listOperation || createOperation || cancelOperation) return false;
    cursor = cursorStack.at(-1) ?? null;
    cursorStack = cursorStack.slice(0, -1);
    nextCursor = null;
    jobs = Object.freeze([]);
    selectJobInternal("");
    listState = "loading";
    return true;
  }

  function nextTaskPage() {
    if (!taskNextCursor || detailOperation || createOperation || cancelOperation) return false;
    clearResult();
    taskCursorStack = [...taskCursorStack, taskCursor];
    taskCursor = taskNextCursor;
    taskNextCursor = null;
    tasks = Object.freeze([]);
    detailState = "loading";
    return true;
  }

  function previousTaskPage() {
    if (!taskCursorStack.length || detailOperation || createOperation || cancelOperation) return false;
    clearResult();
    taskCursor = taskCursorStack.at(-1) ?? null;
    taskCursorStack = taskCursorStack.slice(0, -1);
    taskNextCursor = null;
    tasks = Object.freeze([]);
    detailState = "loading";
    return true;
  }

  function openCreate(owner) {
    if (!currentCapabilityOwner(owner, "create") || createOperation || cancelOperation) return false;
    createOpen = true;
    createState = "idle";
    createError = null;
    createFeedback = null;
    return true;
  }

  function closeCreate(owner) {
    if (!currentCapabilityOwner(owner, "create") || createOperation) return false;
    createOpen = false;
    createState = "idle";
    createError = null;
    return true;
  }

  function beginCreate(owner) {
    const key = keyFromOwner(owner);
    if (
      !currentCapabilityOwner(owner, "create")
      || !key
      || !createOpen
      || createOptionsState !== "ready"
      || createOperation
      || cancelOperation
    ) return null;
    createGeneration += 1;
    createOperation = Object.freeze({
      ownerKey: key,
      generation: createGeneration,
      controller: new AbortController(),
    });
    createState = "submitting";
    createError = null;
    createFeedback = null;
    return createOperation;
  }

  function isCreateCurrent(operation, owner) {
    return mutationCurrent(operation, createOperation, createGeneration, owner, "create");
  }

  function acceptCreate(operation, payload, owner) {
    if (
      !isCreateCurrent(operation, owner)
      || payload?.state !== "ok"
      || !JOB_ID.test(String(payload.job?.batch_job_id ?? ""))
    ) return false;
    createState = "success";
    createError = null;
    createOpen = false;
    createFeedback = Object.freeze({
      code: "batch.created",
      requestId: safeChain(payload.request_id),
      batchJobId: payload.job.batch_job_id,
    });
    return true;
  }

  function rejectCreate(operation, error, owner) {
    if (!isCreateCurrent(operation, owner)) return false;
    createState = "error";
    createError = fixedError(error, "batch.worker_unavailable");
    return true;
  }

  function finishCreate(operation) {
    if (createOperation === operation) createOperation = null;
  }

  function beginCancel(owner, batchJobId) {
    const key = keyFromOwner(owner);
    const normalized = String(batchJobId ?? "");
    if (
      !currentCapabilityOwner(owner, "write")
      || !key
      || !JOB_ID.test(normalized)
      || normalized !== selectedJobId
      || !["queued", "running"].includes(job?.status)
      || cancelOperation
      || createOperation
    ) return null;
    cancelGeneration += 1;
    cancelOperation = Object.freeze({
      ownerKey: key,
      generation: cancelGeneration,
      batchJobId: normalized,
      controller: new AbortController(),
    });
    cancelState = "submitting";
    cancelError = null;
    cancelFeedback = null;
    return cancelOperation;
  }

  function isCancelCurrent(operation, owner) {
    return mutationCurrent(operation, cancelOperation, cancelGeneration, owner, "write")
      && operation.batchJobId === selectedJobId;
  }

  function acceptCancel(operation, payload, owner) {
    if (
      !isCancelCurrent(operation, owner)
      || payload?.state !== "ok"
      || payload.job?.batch_job_id !== operation.batchJobId
    ) return false;
    job = payload.job;
    jobs = Object.freeze(jobs.map((candidate) => (
      candidate.batch_job_id === payload.job.batch_job_id ? payload.job : candidate
    )));
    cancelState = "success";
    cancelError = null;
    cancelFeedback = Object.freeze({
      code: "batch.cancelled",
      requestId: safeChain(payload.request_id),
      batchJobId: payload.job.batch_job_id,
    });
    return true;
  }

  function rejectCancel(operation, error, owner) {
    if (!isCancelCurrent(operation, owner)) return false;
    cancelState = "error";
    cancelError = fixedError(error, "batch.worker_unavailable");
    return true;
  }

  function finishCancel(operation) {
    if (cancelOperation === operation) cancelOperation = null;
  }

  function isActionBusy() {
    return Boolean(createOperation || cancelOperation);
  }

  function blocksShellDomReplacement() {
    return Boolean(createOpen || createOperation || cancelOperation || ["loading", "ready"].includes(resultState));
  }

  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      status,
      jobs,
      selectedJobId,
      job,
      tasks,
      detailState,
      detailError,
      resultState,
      resultError,
      resultTaskId,
      resultRequestId,
      createOpen,
      createOptionsState,
      createOptionsError,
      createOptions,
      createState,
      createError,
      createFeedback,
      cancelState,
      cancelError,
      cancelFeedback,
      canCreate: ownerCanCreate,
      canCancel: ownerCanCancel,
      canReadResults: ownerCanReadResults,
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(nextCursor),
      canTaskPrevious: taskCursorStack.length > 0,
      canTaskNext: Boolean(taskNextCursor),
    });
  }

  function validOperationOwner(key, refreshGeneration) {
    return Boolean(
      key
      && key === ownerKey
      && Number.isSafeInteger(refreshGeneration)
      && refreshGeneration >= 1
    );
  }

  function currentCapabilityOwner(owner, capability) {
    const key = keyFromOwner(owner);
    if (!key || key !== ownerKey) return false;
    if (capability === "create") return ownerCanCreate && owner?.canCreate === true;
    if (capability === "write") return ownerCanCancel && owner?.canCancel === true;
    return false;
  }

  function mutationCurrent(operation, current, generation, owner, capability) {
    return Boolean(
      operation
      && operation === current
      && !operation.controller.signal.aborted
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.generation === generation
      && currentCapabilityOwner(owner, capability)
    );
  }

  function acceptCreateOptionsInternal(value) {
    if (!ownerCanCreate) {
      createOptions = emptyCreateOptions();
      createOptionsState = "idle";
      createOptionsError = null;
      return;
    }
    const normalized = normalizeCreateOptions(value);
    if (!normalized) {
      createOptions = emptyCreateOptions();
      createOptionsState = "error";
      createOptionsError = Object.freeze({ code: "batch.options_unavailable", requestId: "-" });
      return;
    }
    createOptions = normalized;
    createOptionsState = "ready";
    createOptionsError = null;
  }

  function selectJobInternal(value) {
    abortOperation(detailOperation);
    detailOperation = null;
    selectedJobId = value;
    job = null;
    tasks = Object.freeze([]);
    taskCursor = null;
    taskCursorStack = [];
    taskNextCursor = null;
    detailState = value ? "loading" : "idle";
    detailError = null;
    cancelState = "idle";
    cancelError = null;
    cancelFeedback = null;
    clearResult();
  }

  function clearResultDomAndMetadata() {
    try {
      clearResultDom();
    } finally {
      resultState = "idle";
      resultError = null;
      resultTaskId = "";
      resultRequestId = "";
    }
  }

  function clearResult() {
    clearResultDomAndMetadata();
    abortOperation(resultOperation);
    resultOperation = null;
  }

  function resetListPage() {
    abortOperation(listOperation);
    listOperation = null;
    jobs = Object.freeze([]);
    cursor = null;
    cursorStack = [];
    nextCursor = null;
    selectJobInternal("");
    listState = "loading";
    listError = null;
  }

  function resetAll() {
    clearResult();
    abortOperation(listOperation);
    abortOperation(detailOperation);
    abortOperation(createOperation);
    abortOperation(cancelOperation);
    listOperation = null;
    detailOperation = null;
    createOperation = null;
    cancelOperation = null;
    jobs = Object.freeze([]);
    selectedJobId = "";
    job = null;
    tasks = Object.freeze([]);
    status = "";
    cursor = null;
    cursorStack = [];
    nextCursor = null;
    taskCursor = null;
    taskCursorStack = [];
    taskNextCursor = null;
    listState = "loading";
    listError = null;
    detailState = "idle";
    detailError = null;
    createOpen = false;
    createOptionsState = "idle";
    createOptionsError = null;
    createOptions = emptyCreateOptions();
    createState = "idle";
    createError = null;
    createFeedback = null;
    cancelState = "idle";
    cancelError = null;
    cancelFeedback = null;
  }

  return Object.freeze({
    enter,
    clear,
    query,
    listQuery,
    taskQuery,
    setStatus,
    beginList,
    isListCurrent,
    acceptList,
    rejectList,
    finishList,
    selectJob,
    beginDetail,
    isDetailCurrent,
    acceptDetail,
    rejectDetail,
    finishDetail,
    beginResult,
    isResultCurrent,
    acceptResult,
    rejectResult,
    finishResult,
    invalidateResult,
    nextPage,
    previousPage,
    nextTaskPage,
    previousTaskPage,
    openCreate,
    closeCreate,
    beginCreate,
    isCreateCurrent,
    acceptCreate,
    rejectCreate,
    finishCreate,
    beginCancel,
    isCancelCurrent,
    acceptCancel,
    rejectCancel,
    finishCancel,
    isActionBusy,
    blocksShellDomReplacement,
    snapshot,
  });
}

function operationTicket(ownerKey, generation, refreshGeneration) {
  return Object.freeze({
    ownerKey,
    generation,
    refreshGeneration,
    controller: new AbortController(),
  });
}

function isCurrent(operation, current, generation, ownerKey, refreshGeneration) {
  return Boolean(
    operation
    && operation === current
    && !operation.controller.signal.aborted
    && operation.ownerKey === ownerKey
    && operation.generation === generation
    && operation.refreshGeneration === refreshGeneration
  );
}

function abortOperation(operation) {
  operation?.controller?.abort();
}

function keyFromOwner(owner) {
  if (
    !owner
    || typeof owner.readIdentity !== "string"
    || !owner.readIdentity
    || typeof owner.navigationOwner !== "string"
    || !owner.navigationOwner
    || owner.navigation !== NAVIGATION_ID
  ) return "";
  return JSON.stringify([owner.readIdentity, owner.navigationOwner]);
}

function mirroredTenantId(authContext) {
  const userTenant = String(authContext?.user?.tenant_id ?? "").trim();
  const tenant = String(authContext?.tenant?.tenant_id ?? userTenant).trim();
  return userTenant && tenant === userTenant ? tenant : "";
}

function normalizePermissionCodes(value) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) return null;
  const normalized = [...value].sort();
  return new Set(normalized).size === normalized.length ? normalized : null;
}

function normalizeCreateOptions(value) {
  if (
    !value
    || !Array.isArray(value.projects)
    || !Array.isArray(value.environments)
    || !Array.isArray(value.modelAliases)
    || !Array.isArray(value.files)
    || value.files.length > 100
  ) return null;
  const environments = value.environments.filter((item) => item?.status === "active").map((item) => {
    if (!resourceId(item.environment_id) || !safeLabel(item.name) || !safeLabel(item.code, 80)) return null;
    return Object.freeze({
      environment_id: item.environment_id,
      code: item.code,
      name: item.name,
    });
  });
  if (environments.some((item) => !item) || !unique(environments, "environment_id")) return null;
  const environmentByCode = new Map(environments.map((item) => [item.code, item.environment_id]));
  const environmentIds = new Set(environments.map((item) => item.environment_id));
  const projects = value.projects.filter((item) => item?.status === "active").map((item) => {
    if (!resourceId(item.project_id) || !safeLabel(item.name)) return null;
    return Object.freeze({ project_id: item.project_id, name: item.name });
  });
  if (projects.some((item) => !item) || !unique(projects, "project_id")) return null;
  const modelAliases = value.modelAliases.filter((item) => item?.status === "active").map((item) => {
    const environmentId = resourceId(item.environment_id)
      ? item.environment_id
      : environmentByCode.get(item.environment);
    const label = item.display_name || item.alias;
    if (
      !resourceId(item.model_alias_id)
      || !environmentId
      || !environmentIds.has(environmentId)
      || !safeLabel(label)
    ) return null;
    return Object.freeze({
      model_alias_id: item.model_alias_id,
      environment_id: environmentId,
      label,
    });
  });
  if (modelAliases.some((item) => !item) || !unique(modelAliases, "model_alias_id")) return null;
  const files = value.files.map((item) => {
    if (
      !FILE_ID.test(String(item?.file_id ?? ""))
      || item?.status !== "ready"
      || item?.extract?.status !== "ready"
      || !safeLabel(item.original_name, 255)
    ) return null;
    return Object.freeze({ file_id: item.file_id, name: item.original_name });
  });
  if (files.some((item) => !item) || !unique(files, "file_id")) return null;
  return Object.freeze({
    projects: Object.freeze(projects),
    environments: Object.freeze(environments),
    modelAliases: Object.freeze(modelAliases),
    files: Object.freeze(files),
  });
}

function emptyCreateOptions() {
  return Object.freeze({
    projects: Object.freeze([]),
    environments: Object.freeze([]),
    modelAliases: Object.freeze([]),
    files: Object.freeze([]),
  });
}

function unique(values, key) {
  return new Set(values.map((item) => item[key])).size === values.length;
}

function resourceId(value) {
  return RESOURCE_ID.test(String(value ?? ""));
}

function safeLabel(value, maximum = 160) {
  return typeof value === "string"
    && Boolean(value.trim())
    && value.length <= maximum
    && !/[\u0000-\u001f\u007f]/.test(value);
}

function safeChain(value) {
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(String(value ?? "")) ? value : "-";
}

function fixedError(error, fallbackCode) {
  const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(String(error?.code ?? ""))
    ? error.code
    : fallbackCode;
  const requestId = safeChain(error?.requestId);
  return Object.freeze({ code, requestId });
}

export function isBatchAbortError(error) {
  return error?.name === "AbortError";
}
