/**
 * PAGE-091 契约测试报告的筛选、分页、详情与异步 owner 状态机。
 *
 * 所有状态仅驻留当前页面内存；opaque cursor、Token、租户身份摘要和 AbortController 不会进入
 * snapshot、DOM、URL、Storage 或日志。后端仍是 RBAC 与租户隔离的最终边界。
 */

import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";

/** 初始页固定展示合并门禁，真实探测只能作为只读分段查看，浏览器没有执行入口。 */
const DEFAULT_FILTERS = Object.freeze({
  suite_kind: "merge_gate",
  status: "",
  mode: "",
  limit: 50,
});
/** suite、状态和模式白名单必须与只读 API 枚举一致，禁止自由值扩大查询语义。 */
const SUITE_KINDS = new Set(["merge_gate", "probe"]);
const STATUSES = new Set(["", "passed", "failed"]);
const MODES = new Set(["", "mock", "gateway_probe"]);

/**
 * 从后端认证快照生成完整页面身份。
 * tenant、user、Session、Session revision、RBAC revision 和 permission fingerprint 任一缺失都会失败关闭。
 */
export function adapterContractRunIdentitySnapshot(authContext) {
  // 复用审计过的权限摘要，避免 PAGE-091 根据角色名称自行补权或漏掉 revision 变化。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PROVIDER_HEALTH_READ);
}

/** 规范化页面内存筛选；未知值回退固定默认值，且永远不接受 tenant_id。 */
export function canonicalAdapterContractRunFilters(filters = {}) {
  const suiteKind = String(filters.suite_kind ?? DEFAULT_FILTERS.suite_kind).trim().toLowerCase();
  const status = String(filters.status ?? "").trim().toLowerCase();
  const mode = String(filters.mode ?? "").trim().toLowerCase();
  const limit = Number.isSafeInteger(filters.limit) && filters.limit >= 1 && filters.limit <= 100
    ? filters.limit
    : DEFAULT_FILTERS.limit;
  return Object.freeze({
    suite_kind: SUITE_KINDS.has(suiteKind) ? suiteKind : DEFAULT_FILTERS.suite_kind,
    status: STATUSES.has(status) ? status : "",
    mode: MODES.has(mode) ? mode : "",
    limit,
  });
}

/** 为列表 owner 生成固定顺序筛选键；键不含 cursor、Token、tenant 或响应内容。 */
export function adapterContractRunFilterKey(filters = {}) {
  const normalized = canonicalAdapterContractRunFilters(filters);
  return JSON.stringify([
    ["suite_kind", normalized.suite_kind],
    ["status", normalized.status],
    ["mode", normalized.mode],
    ["limit", normalized.limit],
  ]);
}

/**
 * 创建 PAGE-091 页面内存。
 *
 * 列表和详情分别持有 AbortController，但共享 generation；筛选、分页、离页或身份变化会先推进
 * generation，再取消请求，因此忽略 AbortSignal 的测试 stub 或浏览器迟到回调也无法写回。
 */
export function createAdapterContractRunPageState() {
  let generation = 0;
  let filters = { ...DEFAULT_FILTERS };
  let cursor = null;
  let cursorStack = [];
  let page = null;
  let latestMergeGate = null;
  let listError = null;
  let detail = null;
  let detailError = null;
  let selectedRunId = null;
  let listOperation = null;
  let detailOperation = null;

  /** 取消当前列表和详情并推进代次；调用方随后可保留或销毁已净化页面数据。 */
  function invalidateOperations() {
    generation += 1;
    listOperation?.controller.abort();
    detailOperation?.controller.abort();
    listOperation = null;
    detailOperation = null;
  }

  /** 完整销毁上一身份事实，避免上一租户列表或 case 在新 Session 中短暂重现。 */
  function clear() {
    invalidateOperations();
    filters = { ...DEFAULT_FILTERS };
    cursor = null;
    cursorStack = [];
    page = null;
    latestMergeGate = null;
    listError = null;
    detail = null;
    detailError = null;
    selectedRunId = null;
  }

  /** 应用新筛选并回第一页；详情和 opaque cursor 都只能属于旧筛选快照。 */
  function setFilters(values = {}) {
    invalidateOperations();
    filters = { ...canonicalAdapterContractRunFilters(values) };
    cursor = null;
    cursorStack = [];
    page = null;
    listError = null;
    detail = null;
    detailError = null;
    selectedRunId = null;
    return snapshot();
  }

  /** 返回 API 查询白名单；cursor 只从闭包取出，不会进入可渲染 snapshot。 */
  function query() {
    return Object.freeze({ ...filters, cursor });
  }

  /** 创建列表请求 ticket，冻结完整授权、导航、筛选、Token 和全局刷新代次。 */
  function beginListOperation(owner) {
    const normalized = normalizeListOwner(owner);
    if (!normalized || normalized.filterKey !== adapterContractRunFilterKey(filters)) return null;
    listOperation?.controller.abort();
    // 全页或显式列表刷新会改变可选 run 集合；未完成详情必须同步取消并收起，不能悬挂在旧页。
    if (detailOperation) {
      detailOperation.controller.abort();
      detailOperation = null;
      selectedRunId = null;
      detail = null;
      detailError = null;
    }
    generation += 1;
    listOperation = Object.freeze({
      ...normalized,
      generation,
      controller: new AbortController(),
    });
    listError = null;
    return listOperation;
  }

  /** 成功、401、403 和普通错误都必须通过同一个列表 owner 比较。 */
  function isListOperationCurrent(operation, owner) {
    const current = normalizeListOwner(owner);
    return Boolean(
      operation
      && current
      && listOperation === operation
      && operation.generation === generation
      && !operation.controller.signal.aborted
      && sameOwner(operation, current)
      && operation.filterKey === adapterContractRunFilterKey(filters),
    );
  }

  /** 较老请求不得清理较新 listOperation 引用。 */
  function finishListOperation(operation) {
    if (listOperation === operation && operation?.generation === generation) listOperation = null;
  }

  /**
   * 接受已经过客户端严格白名单的列表。
   * next_cursor 留在闭包；合并门禁摘要只由无筛选的第一页更新，避免状态筛选或翻页把
   * “当前结果中的最新一条”误标成租户真正的最新门禁。切到 probe 后仍保留最近可信摘要。
   */
  function acceptPage(payload) {
    const runs = Array.isArray(payload?.adapter_contract_runs)
      ? payload.adapter_contract_runs.map(cloneRun)
      : [];
    page = {
      runs,
      nextCursor: typeof payload?.next_cursor === "string" ? payload.next_cursor : null,
      requestId: safeText(payload?.request_id, 256) || "-",
      traceId: safeText(payload?.trace_id, 256) || "-",
    };
    listError = null;
    if (
      filters.suite_kind === "merge_gate"
      && filters.status === ""
      && filters.mode === ""
      && cursor === null
      && runs.length
    ) {
      latestMergeGate = cloneRun([...runs].sort(compareCompletedAt)[0]);
    }
    if (selectedRunId && !runs.some((run) => run.adapter_contract_run_id === selectedRunId)) {
      selectedRunId = null;
      detail = null;
      detailError = null;
      detailOperation?.controller.abort();
      detailOperation = null;
    }
    return snapshot();
  }

  /**
   * 保存公开列表错误。
   * 403 表示当前 owner 已失去后端读取权限，必须立即销毁先前授权的数据；503 等可用性故障则保留
   * 已经严格净化的页面作为 partial，方便用户在服务恢复前继续只读扫描已有结果。
  */
  function rejectList(error) {
    const permissionDenied = error?.status === 403 || error?.code === "auth.permission_denied";
    const nextError = publicError(
      error,
      permissionDenied ? "auth.permission_denied" : "adapter.contract_storage_unavailable",
      permissionDenied ? "当前用户无权查看契约测试报告" : "契约测试运行暂不可用",
    );
    // 后端权限是最终边界；403 到达后不能依赖稍后的导航刷新清理旧 DOM，也不能保留详情或分页游标。
    if (permissionDenied) {
      invalidateOperations();
      cursor = null;
      cursorStack = [];
      page = null;
      latestMergeGate = null;
      detail = null;
      detailError = null;
      selectedRunId = null;
    }
    listError = nextError;
    return snapshot();
  }

  /** 进入后端签发的下一页，opaque cursor 只写闭包栈。 */
  function nextPage() {
    if (!page?.nextCursor) return false;
    invalidateOperations();
    cursorStack = [...cursorStack, cursor];
    cursor = page.nextCursor;
    page = null;
    listError = null;
    selectedRunId = null;
    detail = null;
    detailError = null;
    return true;
  }

  /** 回到闭包中的上一页；cursor 原值从不投影为 DOM data 属性。 */
  function previousPage() {
    if (!cursorStack.length) return false;
    invalidateOperations();
    cursor = cursorStack[cursorStack.length - 1] ?? null;
    cursorStack = cursorStack.slice(0, -1);
    page = null;
    listError = null;
    selectedRunId = null;
    detail = null;
    detailError = null;
    return true;
  }

  /**
   * 为当前页中的 run 创建详情 ticket。
   * DOM 篡改出的 ID 或页外 ID 不会创建 AbortController，也不会触发网络请求。
   */
  function beginDetailOperation(owner, adapterContractRunId) {
    const normalized = normalizeListOwner(owner);
    const runId = String(adapterContractRunId ?? "");
    if (
      !normalized
      || normalized.filterKey !== adapterContractRunFilterKey(filters)
      || !page?.runs.some((run) => run.adapter_contract_run_id === runId)
    ) return null;
    // 详情只能绑定当前已展示列表；若后台刷新尚未完成，先取消它，避免新列表覆盖刚选中的详情 owner。
    listOperation?.controller.abort();
    listOperation = null;
    detailOperation?.controller.abort();
    generation += 1;
    selectedRunId = runId;
    detail = null;
    detailError = null;
    detailOperation = Object.freeze({
      ...normalized,
      adapterContractRunId: runId,
      generation,
      controller: new AbortController(),
    });
    return detailOperation;
  }

  /** 详情接纳同时比较 run ID、完整 owner、页面当前筛选和共享 generation。 */
  function isDetailOperationCurrent(operation, owner) {
    const current = normalizeListOwner(owner);
    return Boolean(
      operation
      && current
      && detailOperation === operation
      && operation.generation === generation
      && !operation.controller.signal.aborted
      && selectedRunId === operation.adapterContractRunId
      && sameOwner(operation, current)
      && operation.filterKey === adapterContractRunFilterKey(filters),
    );
  }

  /** 接受严格详情 DTO；父 run 必须仍等于当前选中对象。 */
  function acceptDetail(payload) {
    if (payload?.adapter_contract_run?.adapter_contract_run_id !== selectedRunId) return snapshot();
    detail = {
      run: cloneRun(payload.adapter_contract_run),
      cases: Array.isArray(payload.cases) ? payload.cases.map(cloneCase) : [],
      requestId: safeText(payload.request_id, 256) || "-",
      traceId: safeText(payload.trace_id, 256) || "-",
    };
    detailError = null;
    return snapshot();
  }

  /** 详情可用性错误独立展示；详情 403 则证明读取权限已撤销，必须复用列表级完整销毁。 */
  function rejectDetail(error) {
    if (error?.status === 403 || error?.code === "auth.permission_denied") return rejectList(error);
    detail = null;
    detailError = publicError(error, "adapter.contract_storage_unavailable", "契约测试详情暂不可用");
    return snapshot();
  }

  /** 当前详情完成后释放引用，旧请求不能清除后来点击创建的 ticket。 */
  function finishDetailOperation(operation) {
    if (detailOperation === operation && operation?.generation === generation) detailOperation = null;
  }

  /** 收起详情会推进 generation 并取消未完成 GET，防止关闭后被迟到结果重新展开。 */
  function closeDetail() {
    invalidateOperations();
    selectedRunId = null;
    detail = null;
    detailError = null;
    return snapshot();
  }

  /**
   * 构造只含安全可展示字段的快照。
   * Token、tenant identity、cursor、AbortController 和 owner generation 明细均被故意排除。
   */
  function snapshot() {
    const runs = page?.runs ?? [];
    return {
      state: page ? (listError ? "partial" : "ok") : (listError ? "error" : "loading"),
      filters: { ...filters },
      adapter_contract_runs: runs.map(cloneRun),
      latest_merge_gate: latestMergeGate ? cloneRun(latestMergeGate) : null,
      request_id: page?.requestId ?? listError?.requestId ?? "-",
      trace_id: page?.traceId ?? "-",
      error: listError ? { ...listError } : null,
      selected_run_id: selectedRunId,
      detail: detail ? {
        adapter_contract_run: cloneRun(detail.run),
        cases: detail.cases.map(cloneCase),
        request_id: detail.requestId,
        trace_id: detail.traceId,
      } : null,
      detail_error: detailError ? { ...detailError } : null,
      detail_loading: Boolean(selectedRunId && detailOperation && !detail && !detailError),
      can_previous: cursorStack.length > 0,
      can_next: Boolean(page?.nextCursor),
      refreshing: Boolean(listOperation),
    };
  }

  return {
    clear,
    setFilters,
    query,
    beginListOperation,
    isListOperationCurrent,
    finishListOperation,
    acceptPage,
    rejectList,
    nextPage,
    previousPage,
    beginDetailOperation,
    isDetailOperationCurrent,
    acceptDetail,
    rejectDetail,
    finishDetailOperation,
    closeDetail,
    snapshot,
  };
}

/** 判断异常是否来自页面主动取消；只有该错误可以静默丢弃。 */
export function isAdapterContractRunAbortError(error) {
  return error?.name === "AbortError";
}

/** 列表和详情共享 owner 结构；refreshGeneration 阻止旧全页刷新接纳响应。 */
function normalizeListOwner(owner) {
  const identity = String(owner?.identity ?? "");
  const navigation = String(owner?.navigation ?? "");
  const filterKey = String(owner?.filterKey ?? "");
  const token = String(owner?.token ?? "");
  const refreshGeneration = owner?.refreshGeneration;
  return identity
    && navigation === "adapter-contract-runs"
    && filterKey
    && token
    && Number.isSafeInteger(refreshGeneration)
    ? { identity, navigation, filterKey, token, refreshGeneration }
    : null;
}

/** 比较 owner 的全部可变维度；Token 只做内存等值比较。 */
function sameOwner(operation, current) {
  return operation.identity === current.identity
    && operation.navigation === current.navigation
    && operation.filterKey === current.filterKey
    && operation.token === current.token
    && operation.refreshGeneration === current.refreshGeneration;
}

/** 列表默认按完成时间倒序；非法时间在客户端已被拒绝，此处只做稳定选择。 */
function compareCompletedAt(left, right) {
  return Date.parse(right.completed_at) - Date.parse(left.completed_at);
}

/** 复制 run 及 failure code 数组，避免渲染调用方改写状态机内部事实。 */
function cloneRun(run) {
  return { ...run, failure_codes: [...(run?.failure_codes ?? [])] };
}

/** case 仅含客户端白名单标量，浅复制即可隔离渲染层修改。 */
function cloneCase(item) {
  return { ...item };
}

/** 把统一错误裁剪为固定公开字段，禁止 diagnosis、响应正文或异常栈进入快照。 */
function publicError(error, fallbackCode, fallbackMessage) {
  const code = safeText(error?.code, 160);
  const requestId = safeText(error?.requestId, 256);
  const hasPublicContract = Boolean(code && requestId);
  return {
    code: code || fallbackCode,
    message: hasPublicContract ? (safeText(error?.message, 500) || fallbackMessage) : fallbackMessage,
    requestId: requestId || "-",
  };
}

/** 只接受有界无控制字符文本；复杂对象和正文不会被隐式 String 化。 */
function safeText(value, maximumLength) {
  if (typeof value !== "string" || value.length > maximumLength) return "";
  return /[\u0000-\u001f\u007f]/.test(value) ? "" : value.trim();
}
