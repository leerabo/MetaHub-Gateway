/**
 * PAGE-105 授权 owner 与私有异步页面状态。
 *
 * cursor、权限指纹、Controller 和创建幂等 owner 只驻留闭包；snapshot 仅投影可展示事实，
 * tenant、Session 或 RBAC 快照变化时先清空再取消旧请求，迟到响应不能复活旧页面。
 */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasEveryPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";
import { workspaceResourcePageOwner } from "../../lib/workspaceResourcePage.js";
import { workspaceConsoleSessionFromShell } from "../../lib/workspaceConsoleSession.js";

// 页面只接受后端冻结的 experiments 导航、实验 ID、资源 ID 和单向状态枚举。
const NAVIGATION_ID = "experiments";
const EXPERIMENT_ID = /^exp_[0-9a-f]{32}$/;
const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const PRIVATE_CURSOR = /^[A-Za-z0-9_-]{1,1024}$/;
const STATUSES = new Set(["active", "collecting_feedback", "completed"]);
// 创建能力要求读取入口和五项后端 composite permission 同时存在，缺一项即失败关闭。
const CREATE_PERMISSIONS = Object.freeze([
  PERMISSIONS.EXPERIMENT_READ,
  PERMISSIONS.EXPERIMENT_WRITE,
  PERMISSIONS.PROJECT_READ,
  PERMISSIONS.ENVIRONMENT_READ,
  PERMISSIONS.ROUTING_READ,
  PERMISSIONS.MODEL_READ,
]);
// 页面错误文案固定在本地，任何服务端 message、diagnosis 或异常正文都不会进入 snapshot。
const ERROR_MESSAGES = Object.freeze({
  "auth.permission_denied": "当前账号没有模型评测权限",
  "experiment.input_invalid": "实验配置不合法",
  "experiment.query_invalid": "实验查询条件不合法",
  "experiment.idempotency_required": "创建实验需要幂等键",
  "experiment.model_invalid": "请选择两个不同且可用的 Chat 模型",
  "experiment.not_found": "当前租户中找不到该实验",
  "experiment.idempotency_conflict": "该幂等键已用于其他实验配置",
  "experiment.scope_conflict": "当前作用域已有正在采样的实验",
  "experiment.statistics_unavailable": "实验统计暂不可用",
  "experiment.create_options_unavailable": "创建选项暂不可用",
  "experiment.create_options_empty": "当前没有完整的可用 Project、Environment、Alias 和两个 Chat 模型",
  "experiment.response_invalid": "实验响应合同不完整",
  "experiment.unavailable": "模型评测暂不可用",
});

/** 返回 read owner 身份快照；缺权限、tenant、Session 或 RBAC 指纹时固定为空。 */
export function experimentReadIdentitySnapshot(authContext) {
  // read permission 是 PAGE-105 导航、DOM、state 和所有 GET 的共同最小边界。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.EXPERIMENT_READ);
}

/** 返回创建 composite owner；任一依赖权限缺失时不生成可复用身份。 */
export function experimentCreateIdentitySnapshot(authContext) {
  // 前端只用于避免泄露和误操作，后端仍会在 dependency 与事务内重复校验同一权限矩阵。
  if (!hasEveryPermission(authContext, CREATE_PERMISSIONS)) return "";
  return JSON.stringify(
    CREATE_PERMISSIONS.map((code) => authorizationIdentitySnapshot(authContext, code)),
  );
}

/**
 * 把 auth/me 与后端 navigation 的 tenant、权限集合、revision 和 fingerprint 合并为页面 owner。
 * 导航 target 或 required_permissions 任一漂移都返回 null，绝不依赖前端角色名补权。
 */
export function experimentPageOwner(authContext, consoleShellState, {
  mode = "read", session = workspaceConsoleSessionFromShell(consoleShellState),
} = {}) {
  if (consoleShellState?.workspaceNavigationContract === true || session?.authentication === "workspace") {
    const page = workspaceResourcePageOwner(session, consoleShellState, {
      navigationId: NAVIGATION_ID, permissions: [PERMISSIONS.EXPERIMENT_READ],
    });
    if (!page || !["read", "create"].includes(mode)
      || authContext?.permission_revision !== session.context.permission_revision
      || authContext?.membership_revision !== session.context.membership_revision
      || authContext?.permission_fingerprint !== session.context.permission_fingerprint
      || JSON.stringify(permissionCodesFromAuthContext(authContext))
        !== JSON.stringify(permissionCodesFromAuthContext(session.context))) return null;
    const canCreate = hasEveryPermission(session.context, CREATE_PERMISSIONS);
    if (mode === "create" && !canCreate) return null;
    return Object.freeze({
      readIdentity: page.key,
      createIdentity: canCreate ? JSON.stringify([page.key, CREATE_PERMISSIONS]) : "",
      workspaceId: session.owner.workspaceId,
      navigation: NAVIGATION_ID,
      navigationOwner: page.key,
      canCreate,
    });
  }
  const readIdentity = experimentReadIdentitySnapshot(authContext);
  const createIdentity = experimentCreateIdentitySnapshot(authContext);
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
    || !["read", "create"].includes(mode)
    || (mode === "create" && !createIdentity)
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || item?.target_section !== NAVIGATION_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(requiredPermissions) !== JSON.stringify([PERMISSIONS.EXPERIMENT_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(authPermissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    readIdentity,
    createIdentity,
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
  });
}

/** 创建列表、详情、统计和创建命令互相隔离的 PAGE-105 内存状态机。 */
export function createExperimentPageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let canCreate = false;
  let listState = "loading";
  let listError = null;
  let experiments = Object.freeze([]);
  let status = "";
  let cursor = null;
  let cursorStack = [];
  let nextCursor = null;
  let selectedExperimentId = "";
  let detailState = "idle";
  let detailError = null;
  let experiment = null;
  let resultsState = "idle";
  let resultsError = null;
  let results = null;
  let createOpen = false;
  let createState = "idle";
  let createError = null;
  let createRequestId = "";
  let createOptionsState = "idle";
  let createOptionsError = null;
  let createOptions = emptyOptions();
  let generations = { list: 0, detail: 0, results: 0, create: 0 };
  let operations = { list: null, detail: null, results: null, create: null };

  /** 进入一个完整 owner；无效或漂移 owner 会同步销毁旧事实并 abort 全部 lane。 */
  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    canCreate = owner.canCreate;
    if (!canCreate) clearCreate();
    return true;
  }

  /** 清除全部页面事实、私有 cursor、创建能力与未决 Controller。 */
  function clear() {
    const pendingOperations = Object.values(operations).filter(Boolean);
    ownerKey = "";
    canCreate = false;
    listState = "loading";
    listError = null;
    experiments = Object.freeze([]);
    status = "";
    cursor = null;
    cursorStack = [];
    nextCursor = null;
    selectedExperimentId = "";
    detailState = "idle";
    detailError = null;
    experiment = null;
    resultsState = "idle";
    resultsError = null;
    results = null;
    createOptionsState = "idle";
    createOptionsError = null;
    createOptions = emptyOptions();
    createOpen = false;
    createState = "idle";
    createError = null;
    createRequestId = "";
    operations = { list: null, detail: null, results: null, create: null };
    generations = {
      list: generations.list + 1,
      detail: generations.detail + 1,
      results: generations.results + 1,
      create: generations.create + 1,
    };
    // DOM 清理和 Abort 事件都可能同步执行监听器；两者触发前必须完成全部事实和 owner 擦除。
    try {
      clearDom();
    } finally {
      abortOperations(pendingOperations);
    }
  }

  /** 返回不含 cursor、权限指纹、Controller 或幂等键的只读渲染投影。 */
  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      experiments,
      status,
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(nextCursor),
      selectedExperimentId,
      detailState,
      detailError,
      experiment,
      resultsState,
      resultsError,
      results,
      canCreate,
      createOpen,
      createState,
      createError,
      createRequestId,
      createOptionsState,
      createOptionsError,
      createOptions,
    });
  }

  /** 切换固定状态筛选并回到第一页，同时取消旧列表和详情 owner。 */
  function setStatus(value) {
    if (operations.list || operations.create || createOpen) return false;
    const normalized = STATUSES.has(value) ? value : "";
    if (normalized === status) return false;
    status = normalized;
    resetPageAndSelection();
    return true;
  }

  /** 回到私有 cursor 栈上一页；未决列表或创建期间拒绝重复导航。 */
  function previousPage() {
    if (!cursorStack.length || operations.list || operations.create || createOpen) return false;
    cursor = cursorStack.pop() ?? null;
    experiments = Object.freeze([]);
    listState = "loading";
    clearSelection();
    return true;
  }

  /** 使用后端不透明 next cursor 前进一页，cursor 正文不会进入 snapshot。 */
  function nextPage() {
    if (!nextCursor || operations.list || operations.create || createOpen) return false;
    cursorStack.push(cursor);
    cursor = nextCursor;
    nextCursor = null;
    experiments = Object.freeze([]);
    listState = "loading";
    clearSelection();
    return true;
  }

  /** 仅允许选择当前已验证列表中的实验，并为 detail/results 建立新代次。 */
  function selectExperiment(experimentId) {
    if (!EXPERIMENT_ID.test(String(experimentId ?? ""))) return false;
    if (!experiments.some((item) => item.experiment_id === experimentId)) return false;
    if (selectedExperimentId === experimentId && detailState === "ready") return false;
    abortLane("detail");
    abortLane("results");
    selectedExperimentId = experimentId;
    detailState = "loading";
    detailError = null;
    experiment = null;
    resultsState = "loading";
    resultsError = null;
    results = null;
    return true;
  }

  /** 为当前 owner 和 Shell refresh 代次创建列表 GET ticket，并清除上次列表错误。 */
  function beginList(owner, refreshId) {
    if (!ownerMatches(owner) || !Number.isSafeInteger(refreshId)) return null;
    listState = "loading";
    listError = null;
    return begin("list", owner, { refreshId });
  }

  /** 接受同 owner 列表并独立收敛创建候选；候选异常不能污染只读列表。 */
  function acceptList(operation, response, owner, refreshId, optionPayload) {
    if (!isCurrent("list", operation, owner) || operation.refreshId !== refreshId) return false;
    if (
      !response
      || !Array.isArray(response.experiments)
      || response.experiments.length > 100
      || response.experiments.some((item) => (
        !EXPERIMENT_ID.test(String(item?.experiment_id ?? ""))
        || !item?.model_a
        || !item?.model_b
      ))
      || new Set(response.experiments.map((item) => item.experiment_id)).size
        !== response.experiments.length
      || !validPrivateCursor(response.next_cursor)
    ) return false;
    experiments = Object.freeze(response.experiments.map(freezeExperiment));
    nextCursor = response.next_cursor;
    listState = "ready";
    listError = null;
    if (selectedExperimentId && !experiments.some(
      (item) => item.experiment_id === selectedExperimentId,
    )) clearSelection();
    if (canCreate) {
      const normalized = normalizeOptions(optionPayload);
      const complete = normalized ? hasCompleteOptions(normalized) : false;
      createOptionsState = normalized ? (complete ? "ready" : "empty") : "error";
      createOptionsError = complete
        ? null
        : fixedError(
            normalized
              ? { code: "experiment.create_options_empty" }
              : {
                  code: "experiment.create_options_unavailable",
                  requestId: optionPayload?.requestId,
                },
            normalized
              ? "experiment.create_options_empty"
              : "experiment.create_options_unavailable",
          );
      createOptions = normalized ?? emptyOptions();
    } else {
      createOptionsState = "idle";
      createOptionsError = null;
      createOptions = emptyOptions();
    }
    return true;
  }

  /** 将当前列表失败折叠为固定错误，并清除列表和已选详情。 */
  function rejectList(operation, error, owner) {
    if (!isCurrent("list", operation, owner)) return false;
    listState = "error";
    listError = fixedError(error, "experiment.unavailable");
    experiments = Object.freeze([]);
    clearSelection();
    return true;
  }

  /** 为当前选中实验建立同时读取详情与结果的 ticket。 */
  function beginDetail(owner, refreshId) {
    if (!ownerMatches(owner) || !selectedExperimentId) return null;
    return begin("detail", owner, { refreshId, experimentId: selectedExperimentId });
  }

  /** 为同一选中实验建立独立 results GET ticket，不与详情请求共享 Controller 或 generation。 */
  function beginResults(owner, refreshId) {
    if (!ownerMatches(owner) || !selectedExperimentId) return null;
    return begin("results", owner, { refreshId, experimentId: selectedExperimentId });
  }

  /** 只接受 ID、状态、版本、arm owner 和计数彼此一致的详情/结果组合。 */
  function acceptDetail(detailOperation, resultsOperation, detailResponse, resultsResponse, owner) {
    if (
      !isCurrent("detail", detailOperation, owner)
      || !isCurrent("results", resultsOperation, owner)
    ) return false;
    if (
      detailOperation.experimentId !== resultsOperation.experimentId
      || detailResponse?.experiment?.experiment_id !== detailOperation.experimentId
      || resultsResponse?.experiment_id !== resultsOperation.experimentId
      || !detailResultsConsistent(detailResponse.experiment, resultsResponse)
    ) {
      setDetailFailure({
        code: "experiment.statistics_unavailable",
        requestId: resultsResponse?.request_id ?? detailResponse?.request_id,
      });
      return false;
    }
    experiment = freezeExperiment(detailResponse.experiment);
    results = freezeResults(resultsResponse);
    detailState = "ready";
    resultsState = "ready";
    detailError = null;
    resultsError = null;
    return true;
  }

  /**
   * 将详情或统计任一失败作为同一权威读取失败；仅后台瞬时统计 503 可保留同 owner 已冻结 final 快照。
   */
  function rejectDetail(
    detailOperation,
    resultsOperation,
    error,
    owner,
    { preserveFinalOnStatisticsUnavailable = false } = {},
  ) {
    if (
      !isCurrent("detail", detailOperation, owner)
      || !isCurrent("results", resultsOperation, owner)
    ) return false;
    if (
      !preserveFinalOnStatisticsUnavailable
      || !canRetainFinalDetail(error, detailOperation.experimentId)
    ) setDetailFailure(error);
    // 两条 GET 使用独立 Controller；任一失败后同时取消当前组合，迟到单边结果不能落地。
    detailOperation.controller.abort();
    resultsOperation.controller.abort();
    return true;
  }

  /**
   * 只认精确 statistics 503 和当前列表仍一致的 final 详情；非 final、事实版本漂移或页外 ID 均失败关闭。
   */
  function canRetainFinalDetail(error, experimentId) {
    const listedExperiment = experiments.find((item) => item.experiment_id === experimentId);
    return Boolean(
      error?.status === 503
      && error?.code === "experiment.statistics_unavailable"
      && selectedExperimentId === experimentId
      && detailState === "ready"
      && resultsState === "ready"
      && !detailError
      && !resultsError
      && experiment?.experiment_id === experimentId
      && experiment.status === "completed"
      && results?.experiment_id === experimentId
      && results.is_final === true
      && listedExperiment
      && detailResultsConsistent(experiment, results)
      && detailResultsConsistent(listedExperiment, results)
    );
  }

  /** 把详情/结果组合失败写成单一固定错误，并同步清除两侧半成品。 */
  function setDetailFailure(error) {
    const normalized = fixedError(error, "experiment.statistics_unavailable");
    detailState = "error";
    resultsState = "error";
    detailError = normalized;
    resultsError = normalized;
    experiment = null;
    results = null;
  }

  /** 打开创建 Dialog；候选失败仍允许展示原因和重试，但绝不允许提交。 */
  function openCreate(owner) {
    if (!ownerMatches(owner) || !owner?.canCreate || operations.create) return false;
    createOpen = true;
    createState = "idle";
    createError = null;
    createRequestId = "";
    return true;
  }

  /** 仅允许当前创建 owner 在无 POST 未决时关闭 Dialog 并清空草稿状态。 */
  function closeCreate(owner) {
    if (!ownerMatches(owner) || createState === "submitting") return false;
    clearCreate();
    return true;
  }

  /** 为候选完整且 Dialog 已打开的当前 owner 建立唯一 POST ticket。 */
  function beginCreate(owner) {
    if (
      !ownerMatches(owner)
      || !owner?.canCreate
      || !createOpen
      || createOptionsState !== "ready"
      || operations.create
    ) return null;
    createState = "submitting";
    createError = null;
    return begin("create", owner, {});
  }

  /** 接受当前 POST 成功响应，只保存安全 request_id 并关闭 Dialog。 */
  function acceptCreate(operation, response, owner) {
    if (!isCurrent("create", operation, owner) || !response?.experiment) return false;
    createOpen = false;
    createState = "success";
    createRequestId = safeChain(response.request_id);
    createError = null;
    return true;
  }

  /** 保存固定创建错误，网络结果未知时 Dialog closure 继续复用同一幂等键。 */
  function rejectCreate(operation, error, owner) {
    if (!isCurrent("create", operation, owner)) return false;
    createState = "error";
    createError = fixedError(error, "experiment.unavailable");
    return true;
  }

  /** 创建草稿或 POST 未决时阻止 15 秒 Shell 轮询替换 Dialog DOM。 */
  function blocksShellDomReplacement() {
    return createOpen || createState === "submitting";
  }

  /** 在指定 lane 中取消旧操作并生成绑定 owner 与 generation 的 AbortController。 */
  function begin(lane, owner, extra) {
    abortLane(lane);
    const controller = new AbortController();
    const operation = Object.freeze({
      lane,
      generation: ++generations[lane],
      ownerKey: ownerKeyFor(owner),
      controller,
      ...extra,
    });
    operations[lane] = operation;
    return operation;
  }

  /** 判断异步回调是否仍属于当前 owner、lane、generation 且未被 abort。 */
  function isCurrent(lane, operation, owner) {
    return Boolean(
      operation
      && operations[lane] === operation
      && !operation.controller.signal.aborted
      && operation.generation === generations[lane]
      && operation.ownerKey === ownerKeyFor(owner)
      && ownerMatches(owner),
    );
  }

  /** 仅清除完全相同的当前 operation，避免旧 finally 结束新 ticket。 */
  function finish(lane, operation) {
    if (operations[lane] === operation) operations[lane] = null;
  }

  /** 比较闭包 owner 与实时 auth/navigation owner 的精确键。 */
  function ownerMatches(owner) {
    return Boolean(ownerKey && ownerKey === ownerKeyFor(owner));
  }

  /** 取消创建 POST 并清除 Dialog、错误和成功反馈，不触碰候选目录。 */
  function clearCreate() {
    abortLane("create");
    createOpen = false;
    createState = "idle";
    createError = null;
    createRequestId = "";
  }

  /** 取消详情/结果 GET，并清除所有选中实验展示事实。 */
  function clearSelection() {
    abortLane("detail");
    abortLane("results");
    selectedExperimentId = "";
    detailState = "idle";
    detailError = null;
    experiment = null;
    resultsState = "idle";
    resultsError = null;
    results = null;
  }

  /** 清空筛选分页和选中详情，下一次读取从第一页开始。 */
  function resetPageAndSelection() {
    abortLane("list");
    cursor = null;
    cursorStack = [];
    nextCursor = null;
    experiments = Object.freeze([]);
    listState = "loading";
    clearSelection();
  }

  /** 取消单个 lane 并推进 generation，使迟到 resolve/reject 都失效。 */
  function abortLane(lane) {
    operations[lane]?.controller?.abort();
    operations[lane] = null;
    generations[lane] += 1;
  }

  /** 在页面事实已经擦除后取消捕获的旧 Controller，防止 abort 监听器观察旧租户状态。 */
  function abortOperations(pendingOperations) {
    for (const operation of pendingOperations) operation.controller.abort();
  }

  return Object.freeze({
    enter,
    clear,
    snapshot,
    setStatus,
    previousPage,
    nextPage,
    selectExperiment,
    listQuery: () => ({ status, cursor }),
    beginList,
    acceptList,
    rejectList,
    isListCurrent: (operation, owner) => isCurrent("list", operation, owner),
    finishList: (operation) => finish("list", operation),
    beginDetail,
    beginResults,
    acceptDetail,
    rejectDetail,
    isDetailCurrent: (operation, owner) => isCurrent("detail", operation, owner),
    isResultsCurrent: (operation, owner) => isCurrent("results", operation, owner),
    finishDetail: (operation) => finish("detail", operation),
    finishResults: (operation) => finish("results", operation),
    openCreate,
    closeCreate,
    beginCreate,
    acceptCreate,
    rejectCreate,
    isCreateCurrent: (operation, owner) => isCurrent("create", operation, owner),
    finishCreate: (operation) => finish("create", operation),
    blocksShellDomReplacement,
  });
}

/** 把 read/create 身份、tenant 和后端导航快照冻结为仅存内存的 owner key。 */
function ownerKeyFor(owner) {
  // 缺少任一身份或导航维度时不得沿用已有页面状态。
  if (!owner?.readIdentity || !owner?.navigationOwner || !(owner?.tenantId || owner?.workspaceId)) return "";
  return JSON.stringify([
    owner.readIdentity,
    owner.createIdentity,
    owner.workspaceId ? ["workspace", owner.workspaceId] : ["legacy", owner.tenantId],
    owner.navigationOwner,
  ]);
}

/** 从全局资源读取中裁剪当前可用的创建候选，禁用对象绝不进入 Dialog。 */
function normalizeOptions(value) {
  if (
    !value
    || !Array.isArray(value.projects)
    || !Array.isArray(value.environments)
    || !Array.isArray(value.modelAliases)
    || !Array.isArray(value.models)
  ) return null;
  const projects = optionRows(
    value.projects.filter((row) => row?.status === "active"),
    "project_id",
    ["name", "project_code", "code"],
  );
  const environments = optionRows(
    value.environments.filter((row) => row?.status === "active"),
    "environment_id",
    ["name", "code"],
    ["project_id", "code"],
  );
  const modelAliases = optionRows(
    value.modelAliases.filter((row) => row?.status === "active"),
    "model_alias_id",
    ["alias", "name", "code"],
    ["environment_id", "environment", "project_id"],
  );
  const models = optionRows(
    value.models.filter((row) => row?.status === "active" && row?.capabilities?.chat === true),
    "model_id",
    ["display_name", "name", "internal_model_id", "model_code", "code"],
    ["provider_id"],
  );
  if (![projects, environments, modelAliases, models].every(Boolean)) return null;
  return Object.freeze({ projects, environments, modelAliases, models });
}

/** 校验候选 ID、展示标签、作用域元数据和去重约束，并只保留字段白名单。 */
function optionRows(rows, idField, labelFields, ownerFields = []) {
  const result = [];
  const seen = new Set();
  for (const row of rows) {
    const id = String(row?.[idField] ?? "");
    if (!RESOURCE_ID.test(id) || seen.has(id)) return null;
    const label = labelFields.map((field) => String(row?.[field] ?? "").trim()).find(Boolean) ?? id;
    if (
      !label
      || [...label].length > 160
      || /[\u0000-\u001f\u007f-\u009f]/u.test(label)
    ) return null;
    const owners = {};
    for (const field of ownerFields) {
      const value = row?.[field];
      owners[field] = value === null || value === undefined ? "" : String(value);
      if (owners[field] && !RESOURCE_ID.test(owners[field])) return null;
    }
    seen.add(id);
    result.push(Object.freeze({ id, label, ...owners }));
  }
  return Object.freeze(result);
}

/** 返回冻结空候选，避免调用方持有并修改共享数组。 */
function emptyOptions() {
  return Object.freeze({
    projects: Object.freeze([]),
    environments: Object.freeze([]),
    modelAliases: Object.freeze([]),
    models: Object.freeze([]),
  });
}

/** 判断创建至少有一个作用域和两个不同 Chat 模型可供选择。 */
function hasCompleteOptions(value) {
  if (value.models.length < 2) return false;
  return value.projects.some((project) => value.environments.some((environment) => (
    (!environment.project_id || environment.project_id === project.id)
    && value.modelAliases.some((alias) => scopeOptionsMatch(project, environment, alias))
  )));
}

/** 复核至少一组 Project/Environment/Alias owner 可同时选择，避免生成没有合法 Alias 的死表单。 */
function scopeOptionsMatch(project, environment, alias) {
  if (alias.project_id && alias.project_id !== project.id) return false;
  if (!alias.environment_id && !alias.environment) return true;
  return Boolean(
    (alias.environment_id && alias.environment_id === environment.id)
    || (alias.environment && alias.environment === environment.code),
  );
}

/** 把任意异常裁剪为固定 code/message/request_id，不保存原始异常正文。 */
function fixedError(error, fallbackCode) {
  const candidate = String(error?.code ?? "");
  const code = Object.hasOwn(ERROR_MESSAGES, candidate) ? candidate : fallbackCode;
  return Object.freeze({
    code,
    message: ERROR_MESSAGES[code] ?? ERROR_MESSAGES["experiment.unavailable"],
    requestId: safeChain(error?.requestId),
  });
}

/** 校验并排序导航 permission codes，重复权限使 owner 失败关闭。 */
function normalizePermissionCodes(value) {
  if (!Array.isArray(value)) return null;
  const normalized = value.map(String).sort();
  return normalized.length === new Set(normalized).size ? normalized : null;
}

/** 要求 auth user 与 tenant 镜像字段完全相同，避免跨租户上下文拼接。 */
function mirroredTenantId(authContext) {
  const first = String(authContext?.user?.tenant_id ?? "");
  const second = String(authContext?.tenant?.tenant_id ?? "");
  return first && first === second && RESOURCE_ID.test(first) ? first : "";
}

/** 判断异常是否仅由 owner abort 引起，避免把正常撤权/离页显示为错误。 */
export function isExperimentAbortError(error) {
  return error?.name === "AbortError";
}

/** 校验详情与统计来自同一实验事实版本，并绑定两侧 variant/model/provider/计数。 */
function detailResultsConsistent(experiment, response) {
  if (
    !experiment
    || !response
    || experiment.status !== response.status
    || experiment.facts_version !== response.facts_version
    || !Array.isArray(response.results)
    || response.results.length !== 2
    || (response.completion_reason === "target_reached"
      && experiment.assigned_count !== experiment.target_sample_count)
    || (experiment.status === "active" && response.next_state_at !== experiment.sampling_deadline)
    || (experiment.status === "collecting_feedback"
      && ![experiment.feedback_deadline, experiment.settlement_deadline]
        .includes(response.next_state_at))
  ) return false;
  const arms = new Map(response.results.map((arm) => [arm.variant, arm]));
  return [["a", experiment.model_a], ["b", experiment.model_b]].every(([variant, expected]) => {
    const actual = arms.get(variant);
    return Boolean(
      actual
      && actual.model_id === expected.model_id
      && actual.provider_id === expected.provider_id
      && actual.provider_code === expected.provider_code
      && actual.assigned_count === expected.assigned_count
      && actual.feedback_collection_enabled === experiment.feedback_collection_enabled
    );
  });
}

/** 将链路标识限制为安全字符和长度；缺失或异常值统一显示为短横线。 */
function safeChain(value) {
  const normalized = String(value ?? "");
  return CHAIN_ID.test(normalized) ? normalized : "-";
}

/** 校验 nullable 私有 cursor 的长度与无空白合同，状态机从不解析或快照其正文。 */
function validPrivateCursor(value) {
  return value === null || (typeof value === "string" && PRIVATE_CURSOR.test(value));
}

/** 冻结列表/详情 Experiment 及两个 arm，防止调用方在接收后原地改写 snapshot。 */
function freezeExperiment(value) {
  return Object.freeze({
    ...value,
    model_a: Object.freeze({ ...value.model_a }),
    model_b: Object.freeze({ ...value.model_b }),
  });
}

/** 冻结结果 envelope 与两个 arm，保证渲染期间事实版本和计数不可变。 */
function freezeResults(value) {
  return Object.freeze({
    ...value,
    results: Object.freeze(value.results.map((arm) => Object.freeze({ ...arm }))),
  });
}
