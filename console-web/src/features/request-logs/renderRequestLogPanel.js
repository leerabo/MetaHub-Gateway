/** 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。 */

import { renderRequestFeedbackSection } from "../request-feedback/renderRequestFeedback.js";
import {
  authorizationIdentitySnapshot,
  hasPermission,
  PERMISSIONS,
} from "../../lib/permissions.js";
import { normalizeResponseCacheRequestLogInfo } from "../../lib/responseCache.js";
import { hasWorkspaceConsoleSession } from "../../lib/workspaceConsoleSession.js";

// PAGE-082 只把后端稳定公开标识写入一次性 handoff 属性，非法 ID/目录版本降级为纯文本。
const TENANT_ERROR_MAP_REFERENCE_PATTERN = /^pem_[0-9a-f]{32}$/;
const SAFE_ERROR_MAP_REFERENCE_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;

// 筛选、分页和详情选择只属于当前身份的 RequestLog 页面，会在任何 owner 边界变化时重置。
/**
 * 创建 PAGE-080 默认最近 24 小时窗口。
 * 每次建立或清理 owner 都重新取当前 UTC，避免长期开页后复用启动时的陈旧时间边界。
 */
function defaultRequestLogFilters() {
  const now = Date.now();
  return {
    createdFrom: new Date(now - 24 * 60 * 60 * 1000).toISOString(),
    createdTo: new Date(now).toISOString(),
    limit: 20,
    offset: 0,
  };
}

// 状态文案只显示后端白名单枚举，未知值仍经过统一 HTML 转义后展示。
const STATUS_LABELS = {
  success: "成功",
  failed: "失败",
};

const PROVIDER_LABELS = Object.freeze({
  openai: "OpenAI",
  anthropic: "Anthropic",
  claude: "Anthropic",
  google: "Google",
  gemini: "Google",
  deepseek: "DeepSeek",
  qwen: "通义千问",
  kimi: "Kimi",
  moonshot: "Kimi",
  zhipu: "智谱 AI",
  glm: "智谱 AI",
  grok: "Grok",
  mistral: "Mistral",
  meta: "Meta",
  openrouter: "OpenRouter",
  custom: "自定义模型服务",
});

// PAGE-097 只解释后端冻结的语义风险枚举；未知值仍经过转义显示，不能成为 HTML 或状态推断入口。
const INTENT_STATUS_LABELS = Object.freeze({
  disabled: "未启用",
  pending: "待完成（best-effort）",
  clean: "未检出风险",
  detected: "已检测（旁路观察）",
  blocked: "Provider 前已阻断",
  failed: "分类失败",
  skipped: "未调度",
});
// 分类模式决定同步阻断、异步旁路或手工复核语义，前端不得按状态反向重算模式。
const INTENT_MODE_LABELS = Object.freeze({
  async_light: "异步轻量分类",
  sync_strong: "同步强规则",
  manual_sync: "手工同步分类",
});
// 风险类型只展示冻结 taxonomy，绝不从 Prompt、错误或当前规则重新推导历史结果。
const INTENT_RISK_LABELS = Object.freeze({
  none: "无",
  credential_exfiltration: "凭据索取",
  system_prompt_extraction: "系统提示词提取",
  policy_bypass: "安全策略绕过",
  jailbreak: "越狱与指令覆盖",
});
// 动作标签明确 observe 不等于 block，failed 的 none 也不能误显示为安全放行。
const INTENT_ACTION_LABELS = Object.freeze({
  none: "无动作",
  allow: "放行",
  observe: "旁路观察",
  block: "阻断",
  review: "人工复核",
});

// 终止阶段只使用后端冻结的 terminal_stage_v1 枚举，避免 UI 自行推断并宣称未发生的阶段已通过。
const TERMINAL_STAGE_OPTIONS = [
  ["completed", "已完成"],
  ["authentication", "认证"],
  ["rate_limit", "限流"],
  ["budget", "预算"],
  ["dlp", "内容安全"],
  ["output_safety", "输出安全"],
  ["policy", "策略"],
  ["routing", "路由"],
  ["keypool", "模型服务"],
  ["provider", "模型服务"],
  ["usage_and_finops", "用量与成本"],
  ["logs_and_webhooks", "日志与通知"],
  ["unknown", "未知"],
];
// 该集合只做前端内存白名单收敛；任何未知阶段都清空并交由后端合同最终校验。
const TERMINAL_STAGE_VALUES = new Set(TERMINAL_STAGE_OPTIONS.map(([value]) => value));
// PAGE-099 复用同一 RequestLog 数据流，但导航 ID 必须进入 owner，避免两页迟到响应互相覆盖。
const REQUEST_LOG_NAVIGATIONS = new Set(["request-logs", "output-safety"]);
// PAGE-099 筛选只接受 0063 冻结枚举；未知值清空后仍由后端 strict query 做最终拒绝。
const OUTPUT_SAFETY_FILTER_VALUES = Object.freeze({
  status: new Set(["pending", "not_configured", "completed", "unavailable", "not_applicable"]),
  mode: new Set(["non_stream_full", "stream_window"]),
  exposure: new Set(["none", "partial", "complete", "unknown"]),
  billing: new Set(["not_required", "dispute_pending"]),
});
const REQUEST_LOG_PUBLIC_ERROR_MESSAGES = Object.freeze({
  "request_log.query_audit_unavailable": "调用记录暂时无法读取，请稍后重试。",
  "request_log.unavailable": "调用记录暂时无法读取，请稍后重试。",
  "request_log.query_invalid": "筛选条件无效，请检查后重试。",
  "output_safety.integrity_failed": "这条调用记录的安全详情暂时无法读取，请稍后重试。",
});

/**
 * 从认证上下文构造调用日志 owner 身份。
 * tenant、用户、角色、Session ID 或 revision 任一变化都会淘汰旧列表、详情和迟到响应。
 */
export function requestLogIdentitySnapshot(authContext) {
  // 调用日志 owner 绑定后端读权限与矩阵版本，未知角色或撤权身份不能拥有页面内存。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.REQUEST_LOG_READ);
}

/**
 * 创建调用日志页面的纯内存生命周期仓库。
 * list/detail 分别持有 AbortController，但共享 tenant/user/session/navigation/generation owner；
 * 因此旧 fetch 即使忽略 AbortSignal，也无法把上一租户详情接纳进当前页面。
 */
export function createRequestLogPageMemory() {
  let activeIdentity = "";
  let activeTenantId = "";
  let activeWorkspaceOwner = null;
  let factTenantId = "";
  let activeNavigation = "";
  let generation = 0;
  let listController = null;
  let detailController = null;
  let mutationController = null;
  let activeListTicket = null;
  let activeDetailTicket = null;
  let activeMutationTicket = null;
  let filters = copyRequestLogFilters(defaultRequestLogFilters());
  let selectedRequestId = null;
  let listPayload = null;
  let listError = null;
  let detail = null;
  let detailError = null;
  let actionError = null;
  let savedFilters = [];
  let savedFiltersError = null;

  /** 同步取消 list/detail；controller 与 ticket 都清空，防止迟到 finally 误认当前 owner。 */
  function abortRequests() {
    listController?.abort();
    detailController?.abort();
    mutationController?.abort();
    listController = null;
    detailController = null;
    activeListTicket = null;
    activeDetailTicket = null;
    mutationController = null;
    activeMutationTicket = null;
  }

  /** 清除可能包含上一租户业务事实的列表、详情、错误反馈和可识别筛选值。 */
  function clearData({ resetFilters = true } = {}) {
    if (resetFilters) {
      filters = copyRequestLogFilters(defaultRequestLogFilters());
    }
    selectedRequestId = null;
    listPayload = null;
    listError = null;
    detail = null;
    detailError = null;
    actionError = null;
    savedFilters = [];
    savedFiltersError = null;
  }

  /** 推进 owner 代次后清数据；Abort 只是优化，generation 才是迟到回调的最终门禁。 */
  function invalidate({ resetOwner = false, resetFilters = true } = {}) {
    abortRequests();
    generation += 1;
    clearData({ resetFilters });
    if (resetOwner) {
      activeIdentity = "";
      activeTenantId = "";
      activeWorkspaceOwner = null;
      factTenantId = "";
      activeNavigation = "";
    }
  }

  /** 进入完整 owner；身份、租户或导航变化必须先销毁上一 owner 的全部内存。 */
  function enter({ identity, tenantId, navigation, workspaceOwner = null }) {
    const nextIdentity = requestLogText(identity);
    const nextTenantId = requestLogText(tenantId).trim();
    const nextNavigation = requestLogText(navigation);
    // 不完整身份或非活动导航必须按离页处理，禁止空 owner 继承上一租户的列表和详情。
    const workspace = workspaceOwner
      && hasWorkspaceConsoleSession(workspaceOwner.session)
      && hasPermission(workspaceOwner.session.context, PERMISSIONS.REQUEST_LOG_READ)
      && workspaceOwner.navigation === nextNavigation && workspaceOwner.key === nextIdentity
      && workspaceOwner.key === JSON.stringify([workspaceOwner.session.owner, nextNavigation, workspaceOwner.route]);
    if (!nextIdentity || (!workspace && !nextTenantId) || (workspaceOwner && !workspace)
      || !REQUEST_LOG_NAVIGATIONS.has(nextNavigation)) {
      invalidate({ resetOwner: true });
      return false;
    }
    if (
      nextIdentity !== activeIdentity
      || nextTenantId !== activeTenantId
      || nextNavigation !== activeNavigation
      || workspaceOwner?.session !== activeWorkspaceOwner?.session
    ) {
      invalidate({ resetOwner: true });
      activeIdentity = nextIdentity;
      activeTenantId = nextTenantId;
      activeNavigation = nextNavigation;
      activeWorkspaceOwner = workspaceOwner;
    }
    return true;
  }

  /** 对比异步回调的完整 owner；刷新 ID 还必须同时等于主应用当前代次。 */
  function ownerMatches(ticket, owner = {}) {
    return Boolean(
      ticket
      && ticket.generation === generation
      && ticket.identity === activeIdentity
      && ticket.tenantId === activeTenantId
      && ticket.navigation === activeNavigation
      && ticket.identity === requestLogText(owner.identity)
      && ticket.tenantId === requestLogText(owner.tenantId).trim()
      && ticket.navigation === requestLogText(owner.navigation)
      && ((activeWorkspaceOwner && ticket.kind === "saved-filter-mutation")
        || (ticket.refreshId === owner.refreshId && ticket.refreshId === owner.currentRefreshId))
      && (!activeWorkspaceOwner || (hasWorkspaceConsoleSession(activeWorkspaceOwner.session)
        && owner.workspaceOwner?.session === activeWorkspaceOwner.session
        && owner.workspaceOwner?.key === activeWorkspaceOwner.key)),
    );
  }

  /** 核对列表票据仍是当前唯一读取，不能把旧列表作为新详情的父对象。 */
  function isCurrentList(ticket, owner = {}) {
    return Boolean(
      ownerMatches(ticket, owner)
      && activeListTicket === ticket
      && !ticket.signal.aborted,
    );
  }

  /** 核对详情票据、所选 request_id 和完整身份仍一致。 */
  function isCurrentDetail(ticket, owner = {}) {
    return Boolean(
      ownerMatches(ticket, owner)
      && activeDetailTicket === ticket
      && ticket.requestId === selectedRequestId
      && !ticket.signal.aborted,
    );
  }


  /** 核对 SavedFilter mutation ticket 仍属于当前完整 owner，供成功和异常分支共同复用。 */
  function isCurrentSavedFilterMutation(ticket, owner = {}) {
    return Boolean(
      ownerMatches(ticket, owner)
      && activeMutationTicket === ticket
      && !ticket.signal.aborted,
    );
  }

  return {
    /** 配置筛选或详情选择；交互变化立即取消旧请求并清除旧列表/详情/反馈。 */
    configure({ identity, tenantId, navigation, workspaceOwner, nextFilters, nextSelectedRequestId = null }) {
      if (!enter({ identity, tenantId, navigation, workspaceOwner })) {
        return false;
      }
      abortRequests();
      generation += 1;
      filters = copyRequestLogFilters(nextFilters ?? defaultRequestLogFilters());
      selectedRequestId = normalizeSelectedRequestId(nextSelectedRequestId);
      listPayload = null;
      listError = null;
      detail = null;
      detailError = null;
      actionError = null;
      return true;
    },

    /** 开始当前页列表读取，并把本轮筛选和详情选择冻结进不可变 ticket。 */
    beginList({ identity, tenantId, navigation, workspaceOwner, refreshId }) {
      if (!enter({ identity, tenantId, navigation, workspaceOwner })) {
        return null;
      }
      abortRequests();
      listPayload = null;
      listError = null;
      detail = null;
      detailError = null;
      // 操作错误保留到本轮刷新渲染；下一次配置或 mutation 会显式清除。
      listController = new AbortController();
      const ticket = Object.freeze({
        kind: "list",
        identity: activeIdentity,
        tenantId: activeTenantId,
        navigation: activeNavigation,
        refreshId,
        generation: ++generation,
        filters: Object.freeze(copyRequestLogFilters(filters)),
        selectedRequestId,
        controller: listController,
        signal: listController.signal,
      });
      activeListTicket = ticket;
      return ticket;
    },

    isCurrentList,

    /** 只接纳全部行都属于 ticket 租户的列表，后端异常串租户响应不能进入页面内存。 */
    acceptList(ticket, payload, owner) {
      if (!isCurrentList(ticket, owner)) return false;
      const tenant = activeWorkspaceOwner
        ? factTenantId || requestLogText(payload?.request_logs?.[0]?.tenant_id).trim()
        : ticket.tenantId;
      if (!listPayloadMatchesTenant(payload, tenant)
        || (payload.request_logs.length > 0 && !tenant)) {
        return false;
      }
      if (activeWorkspaceOwner && tenant) factTenantId = tenant;
      listPayload = { ...payload, state: "ok" };
      listError = null;
      // controller 保留到 owner 销毁；完成后的 Abort 是幂等操作，可确保离页时所有旧身份句柄统一失效。
      return true;
    },

    /** 当前列表失败时只保存统一安全错误，不复用上一轮租户列表。 */
    acceptListError(ticket, error, owner) {
      if (!isCurrentList(ticket, owner)) {
        return false;
      }
      listPayload = null;
      listError = safeRequestLogError(error, "调用日志读取失败");
      // 错误响应同样属于当前 owner，不能提前脱离统一的身份生命周期清理。
      return true;
    },


    /** 接纳当前列表 ticket 同批读取的个人筛选；响应不能脱离完整 owner 单独进入页面。 */
    acceptSavedFilters(ticket, payload, owner) {
      if (!isCurrentList(ticket, owner) || !Array.isArray(payload?.saved_filters)) {
        return false;
      }
      savedFilters = payload.saved_filters.map(copySavedFilter);
      savedFiltersError = null;
      return true;
    },

    /** SavedFilter 列表失败不隐藏调用日志，但只保留统一错误字段。 */
    acceptSavedFiltersError(ticket, error, owner) {
      if (!isCurrentList(ticket, owner)) {
        return false;
      }
      savedFilters = [];
      savedFiltersError = safeRequestLogError(error, "保存筛选读取失败");
      return true;
    },

    /** 开始 SavedFilter 变更；ticket 绑定 tenant/user/session/navigation/generation。 */
    beginSavedFilterMutation(owner) {
      if (!enter(owner)) {
        return null;
      }
      mutationController?.abort();
      mutationController = new AbortController();
      const ticket = Object.freeze({
        kind: "saved-filter-mutation",
        identity: activeIdentity,
        tenantId: activeTenantId,
        navigation: activeNavigation,
        refreshId: owner.refreshId,
        generation,
        controller: mutationController,
        signal: mutationController.signal,
      });
      activeMutationTicket = ticket;
      actionError = null;
      return ticket;
    },

    /** Mutation 完成前再次核验完整 owner，迟到成功和迟到错误都会被丢弃。 */
    isCurrentSavedFilterMutation,

    /** 只接纳当前 mutation 的安全错误；成功结果由随后刷新重新从服务端读取。 */
    acceptSavedFilterMutationError(ticket, error, owner) {
      if (!isCurrentSavedFilterMutation(ticket, owner)) {
        return false;
      }
      actionError = safeRequestLogError(error, "保存筛选操作失败");
      return true;
    },

    /** Mutation 结束后销毁活动 ticket，避免重复回调再次改变页面反馈。 */
    finishSavedFilterMutation(ticket, owner) {
      if (!isCurrentSavedFilterMutation(ticket, owner)) {
        return false;
      }
      activeMutationTicket = null;
      mutationController = null;
      return true;
    },

    /** 列表已安全接纳后再开始详情读取；详情拥有独立 AbortController 和同一 generation。 */
    beginDetail(listTicket, owner) {
      if (!isCurrentList(listTicket, owner) || !selectedRequestId) {
        return null;
      }
      detailController?.abort();
      detailController = new AbortController();
      const ticket = Object.freeze({
        kind: "detail",
        identity: listTicket.identity,
        tenantId: listTicket.tenantId,
        navigation: listTicket.navigation,
        refreshId: listTicket.refreshId,
        generation: listTicket.generation,
        requestId: selectedRequestId,
        controller: detailController,
        signal: detailController.signal,
      });
      activeDetailTicket = ticket;
      return ticket;
    },

    isCurrentDetail,

    /** 详情必须同时匹配 tenant_id 与 request_id；同名 request_id 不能跨租户复用旧响应。 */
    acceptDetail(ticket, payload, owner) {
      if (!isCurrentDetail(ticket, owner)) return false;
      const tenant = activeWorkspaceOwner
        ? factTenantId || requestLogText(payload?.request_log?.log?.tenant_id).trim()
        : ticket.tenantId;
      if (!tenant || !detailPayloadMatchesOwner(payload, { ...ticket, tenantId: tenant })) {
        return false;
      }
      if (activeWorkspaceOwner) factTenantId = tenant;
      detail = payload;
      detailError = null;
      actionError = null;
      // 已完成详情仍由当前 owner 持有，登出或切租户时与列表句柄一并失效。
      return true;
    },

    /** 详情错误只属于当前 owner；迟到 401/404/5xx 不能覆盖新租户反馈。 */
    acceptDetailError(ticket, error, owner) {
      if (!isCurrentDetail(ticket, owner)) {
        return false;
      }
      detail = null;
      detailError = safeRequestLogError(error, "调用日志详情读取失败");
      actionError = detailError;
      // 保留失败请求的 controller，确保 owner 销毁语义不因成功或失败分支而不同。
      return true;
    },

    /** 登出、身份变化、离页和 pagehide 共用同一销毁入口。 */
    leave() {
      invalidate({ resetOwner: true });
    },

    /** 返回当前筛选和选择的副本，事件处理器不能直接修改仓库内部对象。 */
    query() {
      return {
        filters: copyRequestLogFilters(filters),
        selectedRequestId,
      };
    },

    /** 只把当前 owner 已接纳的列表/详情组装为 renderer 状态。 */
    snapshot() {
      if (listError) {
        return {
          state: "error",
          ...listError,
          filters: copyRequestLogFilters(filters),
          savedFilters: savedFilters.map(copySavedFilter),
          savedFiltersError,
        };
      }
      if (!listPayload) {
        return null;
      }
      return {
        ...listPayload,
        state: "ok",
        filters: copyRequestLogFilters(filters),
        detail,
        detailError,
        actionError,
        savedFilters: savedFilters.map(copySavedFilter),
        savedFiltersError,
      };
    },
  };
}

/** 复制白名单筛选字段，避免外部对象后续变更影响已冻结请求 owner。 */
function copyRequestLogFilters(value) {
  const source = value && typeof value === "object" ? value : {};
  return {
    requestId: requestLogText(source.requestId).trim(),
    traceId: requestLogText(source.traceId).trim(),
    requestModel: requestLogText(source.requestModel).trim(),
    resolvedModel: requestLogText(source.resolvedModel).trim(),
    virtualKeyId: requestLogText(source.virtualKeyId).trim(),
    createdFrom: requestLogText(source.createdFrom).trim(),
    createdTo: requestLogText(source.createdTo).trim(),
    latencyMsMin: requestLogText(source.latencyMsMin).trim(),
    latencyMsMax: requestLogText(source.latencyMsMax).trim(),
    totalTokensMin: requestLogText(source.totalTokensMin).trim(),
    totalTokensMax: requestLogText(source.totalTokensMax).trim(),
    costAmountMin: requestLogText(source.costAmountMin).trim(),
    costAmountMax: requestLogText(source.costAmountMax).trim(),
    currency: requestLogText(source.currency).trim(),
    terminalStage: TERMINAL_STAGE_VALUES.has(source.terminalStage) ? source.terminalStage : "",
    outputSafetyStatus: OUTPUT_SAFETY_FILTER_VALUES.status.has(source.outputSafetyStatus) ? source.outputSafetyStatus : "",
    outputSafetyMode: OUTPUT_SAFETY_FILTER_VALUES.mode.has(source.outputSafetyMode) ? source.outputSafetyMode : "",
    outputSafetyExposure: OUTPUT_SAFETY_FILTER_VALUES.exposure.has(source.outputSafetyExposure) ? source.outputSafetyExposure : "",
    outputSafetyBilling: OUTPUT_SAFETY_FILTER_VALUES.billing.has(source.outputSafetyBilling) ? source.outputSafetyBilling : "",
    environmentId: requestLogText(source.environmentId).trim(),
    snapshotVersion: requestLogText(source.snapshotVersion).trim(),
    status: requestLogText(source.status).trim(),
    model: requestLogText(source.model).trim(),
    providerCode: requestLogText(source.providerCode).trim(),
    projectId: requestLogText(source.projectId).trim(),
    applicationId: requestLogText(source.applicationId).trim(),
    errorCode: requestLogText(source.errorCode).trim(),
    sortBy: ["created_at", "latency_ms", "total_tokens", "estimated_cost_amount"].includes(source.sortBy) ? source.sortBy : "created_at",
    sortDirection: source.sortDirection === "asc" ? "asc" : "desc",
    selectedSavedFilterId: requestLogText(source.selectedSavedFilterId).trim(),
    limit: positiveRequestLogInteger(source.limit, 20),
    offset: nonnegativeRequestLogInteger(source.offset),
  };
}


/**
 * 将服务端 SavedFilter 恢复为 PAGE-080 UI 筛选。
 * 只读取 v1 白名单字段；tenant、owner 和分页位置不会被服务端对象反向注入查询。
 */
export function requestLogFiltersFromSavedFilter(savedFilter) {
  const raw = savedFilter?.filters ?? parseSavedFilterJson(savedFilter?.filter_json);
  const source = raw && typeof raw === "object" ? raw : {};
  return copyRequestLogFilters({
    requestId: source.request_id,
    traceId: source.trace_id,
    status: source.status,
    requestModel: source.request_model,
    resolvedModel: source.resolved_model,
    providerCode: source.provider_code,
    virtualKeyId: source.virtual_key_id,
    environmentId: source.environment_id,
    projectId: source.project_id,
    applicationId: source.application_id,
    snapshotVersion: source.snapshot_version,
    errorCode: source.error_code,
    createdFrom: source.created_from,
    createdTo: source.created_to,
    latencyMsMin: source.latency_ms_min,
    latencyMsMax: source.latency_ms_max,
    totalTokensMin: source.total_tokens_min,
    totalTokensMax: source.total_tokens_max,
    costAmountMin: source.cost_amount_min,
    costAmountMax: source.cost_amount_max,
    currency: source.currency,
    terminalStage: source.terminal_stage,
    outputSafetyStatus: source.output_safety_status,
    outputSafetyMode: source.output_safety_mode,
    outputSafetyExposure: source.output_safety_exposure,
    outputSafetyBilling: source.output_safety_billing,
    sortBy: savedFilter?.sort_by,
    sortDirection: savedFilter?.sort_direction,
    selectedSavedFilterId: savedFilter?.saved_filter_id,
    limit: 20,
    offset: 0,
  });
}

/** SavedFilter DTO 仅复制页面需要的公开字段，避免未来后端扩展字段被意外渲染。 */
function copySavedFilter(value) {
  return {
    saved_filter_id: requestLogText(value?.saved_filter_id).trim(),
    name: requestLogText(value?.name).trim(),
    filters: value?.filters && typeof value.filters === "object" ? { ...value.filters } : undefined,
    filter_json: requestLogText(value?.filter_json),
    sort_by: requestLogText(value?.sort_by || "created_at"),
    sort_direction: requestLogText(value?.sort_direction || "desc"),
    version: positiveRequestLogInteger(value?.version, 1),
    updated_at: requestLogText(value?.updated_at),
  };
}

/** 数据库 JSON 只用于兼容 DTO 过渡；解析失败返回空对象并由后端下一次查询统一校验。 */
function parseSavedFilterJson(value) {
  try {
    const parsed = JSON.parse(requestLogText(value) || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** request_id 只允许有界非空文本进入详情选择，异常值直接视为未选择。 */
function normalizeSelectedRequestId(value) {
  const normalized = requestLogText(value).trim();
  return normalized && normalized.length <= 160 ? normalized : null;
}

/** 列表每一行都必须显式携带当前 tenant_id，空列表仍是合法租户结果。 */
function listPayloadMatchesTenant(payload, tenantId) {
  return Boolean(
    payload
    && Array.isArray(payload.request_logs)
    && payload.request_logs.every(
      (item) => requestLogText(item?.tenant_id).trim() === tenantId,
    ),
  );
}

/** 详情根对象中的权威日志必须同时绑定当前 tenant 和所选 request_id。 */
function detailPayloadMatchesOwner(payload, ticket) {
  const log = payload?.request_log?.log;
  return Boolean(
    log
    && requestLogText(log.tenant_id).trim() === ticket.tenantId
    && requestLogText(log.request_id).trim() === ticket.requestId,
  );
}

/** 错误内存只保留统一公开字段，不能把响应正文、Header 或异常栈传给 renderer。 */
function safeRequestLogError(error, fallbackMessage) {
  const code = requestLogText(error?.code);
  const publicMessage = REQUEST_LOG_PUBLIC_ERROR_MESSAGES[code];
  return {
    code,
    message: publicMessage ?? requestLogText(error?.message || fallbackMessage),
    requestId: requestLogText(error?.requestId ?? error?.request_id ?? "-") || "-",
  };
}

/** 把分页值限制为正安全整数，异常值回到页面默认上限。 */
function positiveRequestLogInteger(value, fallback) {
  return Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : fallback;
}

/** offset 只接受非负安全整数，避免前端状态先于后端校验产生负分页。 */
function nonnegativeRequestLogInteger(value) {
  return Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : 0;
}

/** 统一把可空身份、筛选和服务端字段转为文本，不在 owner 比较中执行隐式对象序列化。 */
function requestLogText(value) {
  return value === null || value === undefined ? "" : String(value);
}

/**
 * 渲染调用日志面板。
 * 面板只对管理员和开发者可见；真正的权限校验仍由后端 /request-logs 依赖完成。
 */
export function renderRequestLogPanel(requestLogsState, authContext, feedbackState = null, i18n = null) {
  // 缺少后端 request_log.read 时零 DOM，不能按历史角色集合推导权限。
  if (!hasPermission(authContext, PERMISSIONS.REQUEST_LOG_READ)) {
    return "";
  }
  // 该分支影响安全或治理链路，需要用中文说明处理边界。
  if (!requestLogsState) {
    return renderLoadingPanel();
  }
  // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
  if (requestLogsState.state === "error") {
    // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    return renderErrorPanel(requestLogsState);
  }

  const logs = requestLogsState.request_logs ?? [];
  return `
    <div class="panel-heading request-logs-heading">
      <div>
        <h2 id="request-logs-title">调用记录</h2>
        <p>按请求、模型、归属、时间、延迟、Token 和成本组合查询当前组织调用记录。</p>
      </div>
      <span class="request-logs-count">${escapeHtml(logs.length)} / ${escapeHtml(requestLogsState.total ?? 0)}</span>
    </div>
    ${renderActionError(requestLogsState.actionError)}
    ${renderSavedFilterControls(requestLogsState)}
    ${renderFilterForm(requestLogsState.filters ?? {})}
    ${renderLogTable(logs)}
    ${renderPager(requestLogsState)}
    ${requestLogsState.detail || requestLogsState.detailError
      ? renderRequestLogDetail(requestLogsState.detail, requestLogsState.detailError, feedbackState, authContext, i18n, { dialog: true })
      : ""}
  `;
}

/**
 * 渲染加载态，避免接口返回前页面出现空白区域。
 */
function renderLoadingPanel() {
  return `
    <div class="panel-heading">
      <h2 id="request-logs-title">调用记录</h2>
      <p>正在读取当前组织的调用记录。</p>
    </div>
    <div class="empty-state">正在连接调用记录。</div>
  `;
}

/**
 * 普通用户只需要知道读取失败；关联号保留在状态对象和服务端日志中。
 */
function renderErrorPanel(state) {
  const code = requestLogText(state?.code);
  const message = REQUEST_LOG_PUBLIC_ERROR_MESSAGES[code]
    ?? requestLogText(state?.message)
    ?? "调用记录暂时无法读取，请稍后重试。";
  return `
    <div class="panel-heading">
      <h2 id="request-logs-title">调用记录</h2>
      <p>调用记录暂时无法读取，请稍后重试。</p>
    </div>
    <div class="empty-state error-state">
      ${escapeHtml(message)}
    </div>
  `;
}


/**
 * 渲染 PAGE-080 高级查询表单。
 * 浏览器只持有结构化白名单字段；提交时由 client 编码，原始值不会进入 URL 历史或 Storage。
 */
function renderFilterForm(filters) {
  return `
    <form class="request-log-filter-form" id="request-log-filter-form">
      <label><span>状态</span><select name="status">
        ${renderOption("", "全部", filters.status)}
        ${renderOption("success", "成功", filters.status)}
        ${renderOption("failed", "失败", filters.status)}
      </select></label>
      <label><span>请求模型</span><input name="requestModel" value="${escapeAttribute(filters.requestModel ?? filters.model ?? "")}" placeholder="company-default" /></label>
      <label><span>实际模型</span><input name="resolvedModel" value="${escapeAttribute(filters.resolvedModel ?? "")}" placeholder="模型名称" /></label>
      <label><span>开始时间</span><input type="datetime-local" name="createdFrom" value="${escapeAttribute(requestLogDateTimeLocalValue(filters.createdFrom))}" /></label>
      <label><span>结束时间</span><input type="datetime-local" name="createdTo" value="${escapeAttribute(requestLogDateTimeLocalValue(filters.createdTo))}" /></label>

      <details class="request-log-advanced-fields">
        <summary>更多条件</summary>
        <div class="request-log-advanced-grid">
          <label><span>终止阶段</span><select name="terminalStage">
            ${renderOption("", "全部", filters.terminalStage)}
            ${TERMINAL_STAGE_OPTIONS.map(([value, label]) => renderOption(value, label, filters.terminalStage)).join("")}
          </select></label>
          <label><span>延迟下限 (ms)</span><input name="latencyMsMin" type="number" min="0" step="1" value="${escapeAttribute(filters.latencyMsMin ?? "")}" /></label>
          <label><span>延迟上限 (ms)</span><input name="latencyMsMax" type="number" min="0" step="1" value="${escapeAttribute(filters.latencyMsMax ?? "")}" /></label>
          <label><span>Token 下限</span><input name="totalTokensMin" type="number" min="0" step="1" value="${escapeAttribute(filters.totalTokensMin ?? "")}" /></label>
          <label><span>Token 上限</span><input name="totalTokensMax" type="number" min="0" step="1" value="${escapeAttribute(filters.totalTokensMax ?? "")}" /></label>
          <label><span>成本下限</span><input name="costAmountMin" inputmode="decimal" value="${escapeAttribute(filters.costAmountMin ?? "")}" /></label>
          <label><span>成本上限</span><input name="costAmountMax" inputmode="decimal" value="${escapeAttribute(filters.costAmountMax ?? "")}" /></label>
          <label><span>币种</span><select name="currency">
            ${renderOption("", "全部", filters.currency)}
            ${renderOption("CNY", "CNY", filters.currency)}
            ${renderOption("USD", "USD", filters.currency)}
          </select></label>
        </div>
      </details>

      <div class="request-log-query-controls">
        <label><span>排序字段</span><select name="sortBy">
          ${renderOption("created_at", "调用时间", filters.sortBy)}
          ${renderOption("latency_ms", "延迟", filters.sortBy)}
          ${renderOption("total_tokens", "Token", filters.sortBy)}
          ${renderOption("estimated_cost_amount", "成本", filters.sortBy)}
        </select></label>
        <div class="request-log-filter-actions">
          <button type="submit">查询</button>
          <button type="button" id="request-log-refresh" aria-label="刷新最新调用记录">刷新</button>
          <button type="button" id="request-log-filter-reset">清空</button>
        </div>
      </div>
    </form>
  `;
}

/** 渲染当前用户的 SavedFilter 命令区；名称始终经过 HTML 属性转义。 */
function renderSavedFilterControls(state) {
  const items = Array.isArray(state.savedFilters) ? state.savedFilters : [];
  const selectedId = state.filters?.selectedSavedFilterId ?? "";
  const selected = items.find((item) => item.saved_filter_id === selectedId);
  return `
    <section class="request-log-saved-filters" aria-label="保存筛选">
      <label>
        <span>保存筛选</span>
        <select id="request-log-saved-filter-select">
          ${renderOption("", "未选择", selectedId)}
          ${items.map((item) => renderOption(item.saved_filter_id, item.name, selectedId)).join("")}
        </select>
      </label>
      <label class="request-log-saved-filter-name">
        <span>名称</span>
        <input id="request-log-saved-filter-name" aria-label="筛选名称" maxlength="64" value="${escapeAttribute(selected?.name ?? "")}" />
      </label>
      <div class="request-log-saved-filter-actions">
        <button type="button" id="request-log-saved-filter-apply" ${selected ? "" : "disabled"}>应用</button>
        <button type="button" id="request-log-saved-filter-create">保存</button>
        <button type="button" id="request-log-saved-filter-update" ${selected ? "" : "disabled"}>更新</button>
        <button type="button" id="request-log-saved-filter-delete" ${selected ? "" : "disabled"}>删除</button>
      </div>
      ${state.savedFiltersError ? `<span class="request-log-saved-filter-error" role="alert">${escapeHtml(state.savedFiltersError.message ?? "筛选保存失败")}</span>` : ""}
    </section>
  `;
}

/**
 * 把绝对时刻转换为浏览器本地 datetime-local 值。
 * 不能直接删除 ISO 尾部 Z，否则中国时区会把 UTC 00:00 错当成本地 00:00，回传后整体偏移八小时。
 */
export function requestLogDateTimeLocalValue(value) {
  const text = requestLogText(value).trim();
  if (!text) return "";
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return "";
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}T${hours}:${minutes}`;
}

/**
 * 渲染调用日志表格。
 * 表格只显示用户可理解的调用摘要；详情按钮使用隐藏的日志引用查询后端。
 */
function renderLogTable(logs) {
  return `
    <div class="request-log-table" role="table" aria-label="当前组织调用记录">
      <div class="request-log-row request-log-head" role="row">
        <span>状态</span>
        <span>模型</span>
        <span>模型服务</span>
        <span>使用者 / 项目</span>
        <span>延迟</span>
        <span>Token / 成本</span>
        <span>时间</span>
        <span>操作</span>
      </div>
      ${logs.length ? logs.map(renderLogRow).join("") : renderEmptyRow()}
    </div>
  `;
}

/**
 * 渲染单行调用日志。错误码只在失败请求中突出显示。
 */
function renderLogRow(item) {
  return `
    <div class="request-log-row" role="row">
      <span data-label="状态">
        <mark class="status-mark status-${escapeAttribute(item.status)}">${escapeHtml(STATUS_LABELS[item.status] ?? item.status)}</mark>
      </span>
      <span class="request-log-model" data-label="模型">
        <strong>${escapeHtml(item.request_model ?? "-")}</strong>
        <em>${escapeHtml(item.resolved_model ?? "-")}</em>
      </span>
      <span data-label="模型服务">${escapeHtml(providerDisplayName(item))}</span>
      <span class="request-log-scope" data-label="使用者 / 项目">
        <strong>${escapeHtml(ownerDisplayName(item))}</strong>
        <em>${escapeHtml(scopeDisplayName(item))}</em>
      </span>
      <span data-label="延迟">${escapeHtml(formatLatency(item.latency_ms))}</span>
      <span data-label="Token / 成本">${escapeHtml(formatUsageAndCost(item))}</span>
      <time data-label="时间" datetime="${escapeAttribute(item.created_at)}">${escapeHtml(formatDateTime(item.created_at))}</time>
      <span class="request-log-row-actions" data-label="操作">
        <button type="button" data-request-log-detail="${escapeAttribute(item.request_id)}">查看详情</button>
        ${item.has_feedback === true ? `<em class="request-log-feedback-state">已反馈</em>` : ""}
      </span>
    </div>
  `;
}

/**
 * 渲染空列表提示。过滤条件过窄时也会进入这个状态。
 */
function renderEmptyRow() {
  return `
    <div class="request-log-row request-log-empty" role="row">
      <span>当前条件下没有调用记录。</span>
    </div>
  `;
}

/**
 * 渲染分页状态和上一页/下一页按钮。
 */
function renderPager(state) {
  const limit = Number(state.limit ?? 20);
  const offset = Number(state.offset ?? 0);
  const total = Number(state.total ?? 0);
  const canPrev = offset > 0;
  const canNext = state.has_more === true || offset + limit < total;
  const rangeStart = total ? offset + 1 : 0;
  return `
    <div class="request-log-pager" data-limit="${escapeAttribute(limit)}" data-offset="${escapeAttribute(offset)}">
      <button type="button" id="request-log-prev" ${canPrev ? "" : "disabled"}>上一页</button>
      <span>${escapeHtml(rangeStart)}-${escapeHtml(Math.min(offset + limit, total))} / ${escapeHtml(total)}</span>
      <button type="button" id="request-log-next" ${canNext ? "" : "disabled"}>下一页</button>
    </div>
  `;
}

/**
 * 渲染详情区，包含路由决策和用量账本摘要。
 */
export function renderRequestLogDetail(detail, error, feedbackState, authContext, i18n, { dialog = false } = {}) {
  if (error) {
    const errorMarkup = `
      <div class="request-log-detail error-state" role="alert">
        ${escapeHtml(error.message ?? "调用日志详情读取失败")}
      </div>
    `;
    return dialog ? renderRequestLogDialog(errorMarkup, null) : errorMarkup;
  }
  // 该分支影响安全或治理链路，需要用中文说明处理边界。
  if (!detail?.request_log) {
    const emptyMarkup = `
      <div class="request-log-detail empty-state">
        选择一条调用记录查看路由决策和用量详情。
      </div>
    `;
    return dialog ? renderRequestLogDialog(emptyMarkup, null) : emptyMarkup;
  }
  const log = detail.request_log.log;
  const detailMarkup = `
    <div class="request-log-detail">
      <div class="request-log-detail-heading">
        <div><h3>调用详情</h3><p>查看这次调用的结果、实际模型、用量和成本。</p></div>
        <div class="request-log-detail-actions">
          <button type="button" data-request-log-detail-refresh="${escapeAttribute(log.request_id)}">刷新</button>
          ${dialog ? '<button type="button" class="secondary" data-request-log-detail-close>关闭</button>' : ""}
        </div>
      </div>
      ${renderDetailSummary(log, detail.request_log)}
      ${renderBudgetDecision(log, detail.request_log.budget_evaluations ?? [])}
      ${renderRequestFeedbackSection(feedbackState, authContext, i18n)}
      ${dialog
        ? `<div class="request-log-technical-body request-log-technical-body-expanded">
            ${renderFriendlyFailure(log, detail.request_log)}
            ${renderRoutingDecision(detail.request_log.routing_decision, detail.request_log.routing_attempts ?? [])}
            ${renderRoutingAttempts(detail.request_log.routing_attempts ?? [], detail.request_log, { compact: true })}
            ${renderUsage(detail.request_log.usage ?? [])}
            ${renderTechnicalDiagnostics(log, detail.request_log)}
          </div>`
        : renderTechnicalDiagnostics(log, detail.request_log, { includeRouting: true })}
    </div>
  `;
  return dialog ? renderRequestLogDialog(detailMarkup, log.request_id) : detailMarkup;
}

/** 弹框和内嵌详情共享脱敏诊断，避免布局切换遗漏安全结论。 */
function renderTechnicalDiagnostics(log, detail, { includeRouting = false } = {}) {
  return `<details class="request-log-technical-details">
    <summary>查看完整诊断</summary>
    <div class="request-log-technical-body">
      ${renderPolicySnapshotDecision(log)}
      ${renderDlpDecision(log, detail.dlp_matches ?? [])}
      ${renderIntentRisk(detail.intent_risk)}
      ${renderOutputSafetyEvidence(detail.output_safety)}
      ${renderDiagnosisCard(detail.error_diagnosis)}
      ${renderCacheAffinitySelections(detail.cache_affinity_selections ?? [], detail.usage ?? [])}
      ${includeRouting ? "" : (detail.routing_attempts ?? [])
        .map((item) => renderProviderErrorFact(item.provider_error_fact, "attempt")).join("")}
      ${includeRouting ? `${renderRoutingDecision(detail.routing_decision, detail.routing_attempts ?? [])}
        ${renderRoutingAttempts(detail.routing_attempts ?? [], detail)}
        ${renderUsage(detail.usage ?? [])}` : ""}
      ${renderStreamSessions(requestLogStreamSessions(detail), detail)}
    </div>
  </details>`;
}

/** 预计复用与上游实报缓存用量分别展示；不读取绑定、凭据或作用域标识。 */
function renderCacheAffinitySelections(selections, usage) {
  const reasons = {
    binding_warm_reused: "预计复用绑定", binding_rebound: "重新绑定",
    binding_created: "创建绑定", binding_cold_reused: "复用冷绑定",
  };
  const reported = usage.filter((item) => item.cache_tokens != null)
    .map((item) => renderPair("上游实报缓存 Token", item.cache_tokens)).join("");
  return `<section class="request-log-subsection cache-affinity-evidence"><h4>Prompt Cache 亲和</h4>
    <p>预计复用不代表实际缓存命中；缓存用量以上游实报为准。</p>
    ${selections.length ? selections.map((item) => `<dl class="request-log-detail-grid">
      ${renderPair("选择结果", reasons[item.selection_reason] ?? "未识别的选择结果")}
      ${renderPair("配置版本", item.cache_affinity_version == null ? "未提供" : `v${item.cache_affinity_version}`)}
      ${renderPair("目标", item.target_kind === "managed" ? "托管目标" : item.target_kind === "environment" ? "环境目标" : "未提供")}
      ${renderPair("模型", publicModelLabel(item.model_id))}
      ${renderPair("模型服务", PROVIDER_LABELS[item.provider_code] ?? item.provider_code ?? "未提供")}
    </dl>`).join("") : "<p>本次请求未记录 Prompt Cache 亲和选择</p>"}
    ${reported ? `<dl class="request-log-detail-grid">${reported}</dl>` : ""}
  </section>`;
}

function renderRequestLogDialog(content, requestId) {
  return `
    <dialog class="request-log-detail-dialog" aria-modal="true" aria-labelledby="request-log-detail-title">
      <div class="request-log-detail-dialog-card">
        ${content.replace("<h3>调用详情</h3>", '<h3 id="request-log-detail-title">调用详情</h3>')}
      </div>
    </dialog>
  `;
}

// PAGE-106 RequestLog 扩展只解释后端冻结枚举，不能从 Provider cache_tokens 或亲和选择推导命中。
const RESPONSE_CACHE_STATUS_LABELS = Object.freeze({
  disabled: "未启用",
  ineligible: "请求不适用",
  bypassed_experiment: "实验范围旁路",
  unavailable_bypass: "缓存不可用，已旁路",
  corrupt_miss: "缓存损坏，按未命中处理",
  miss_owner: "未命中，已取得填充租约",
  miss_non_owner: "未命中，其他请求正在填充",
  fill_stored: "未命中，已写入缓存",
  fill_skipped: "未命中，未写入缓存",
  hit: "命中",
  hit_blocked: "命中候选被输出安全阻断",
  settlement_failed: "命中结算失败",
});
// 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
const RESPONSE_CACHE_COST_STATUS_LABELS = Object.freeze({
  known: "已确认",
  unavailable: "证据不可用",
  not_applicable: "不适用",
});

/**
 * 渲染独立 Response Cache 事实与四组财务证据。
 * extra/missing/type/tenant/金额等式任一异常都只显示固定合同错误，不读取或回显未知字段。
 */
export function renderResponseCacheEvidence(value, tenantId) {
  const normalized = normalizeResponseCacheRequestLogInfo(value, String(tenantId ?? ""));
  if (normalized === null) {
    return `
      <section class="request-log-subsection response-cache-evidence">
        <div class="response-cache-evidence-heading"><h4>Response Cache</h4><span>无缓存事实</span></div>
      </section>
    `;
  }
  if (normalized === undefined) {
    return `
      <section class="request-log-subsection response-cache-evidence is-invalid" role="alert">
        <div class="response-cache-evidence-heading"><h4>Response Cache</h4><span>投影不可用</span></div>
        <code>response_cache.response_invalid</code>
      </section>
    `;
  }
  const hitLabel = normalized.hit ? "命中" : "未命中 / 旁路";
  return `
    <section class="request-log-subsection response-cache-evidence" data-response-cache-status="${escapeAttribute(normalized.status)}">
      <div class="response-cache-evidence-heading">
        <h4>Response Cache</h4>
        <span class="response-cache-hit-state tone-${normalized.hit ? "hit" : "miss"}">${hitLabel}</span>
      </div>
      <dl class="request-log-detail-grid response-cache-cost-grid">
        ${renderPair("状态", RESPONSE_CACHE_STATUS_LABELS[normalized.status] ?? normalized.status)}
        ${renderPair("策略版本", normalized.cache_policy_version === null ? "-" : `v${normalized.cache_policy_version}`)}
        ${renderPair("命中收费", normalized.charge_cache_hits === null ? "-" : booleanLabel(normalized.charge_cache_hits))}
        ${renderResponseCacheCostPair("源请求生成成本", normalized.source_generation_cost_status, normalized.source_generation_cost_cny)}
        ${renderResponseCacheCostPair("当前参考成本", normalized.current_reference_cost_status, normalized.current_reference_cost_cny)}
        ${renderResponseCacheCostPair("本次节省", normalized.saved_cost_status, normalized.saved_cost_cny)}
        ${renderResponseCacheCostPair("本次计费", normalized.billed_cost_status, normalized.billed_cost_cny)}
      </dl>
    </section>
  `;
}

/** 金额和证据状态保持同一格显示，unknown 不伪造成 0 元。 */
function renderResponseCacheCostPair(label, status, amount) {
  const formatted = status === "known" ? `¥${amount}` : "-";
  return renderPair(
    label,
    `${formatted} · ${RESPONSE_CACHE_COST_STATUS_LABELS[status] ?? status}`,
  );
}

// 输出安全展示只解释后端冻结枚举，不从错误码、DLP 当前规则或已发送字节反向推导扫描结论。
const OUTPUT_SAFETY_SCAN_STATUS_LABELS = Object.freeze({
  pending: "处理中 / 中断待核查",
  not_configured: "未配置",
  completed: "扫描完成",
  unavailable: "扫描不可用",
  not_applicable: "Provider 未产生可扫描输出",
  legacy_unavailable: "历史证据不可用",
});
// 判定标签独立于扫描状态，避免把 unavailable/matched 等二维事实错误折叠为 clean。
const OUTPUT_SAFETY_DECISION_LABELS = Object.freeze({
  none: "无判定",
  clean: "未检出",
  matched: "已匹配（未阻断）",
  blocked: "已阻断",
  unknown: "未知",
});
// 暴露标签表示已持久化的客户可见范围，未知状态必须明确显示而不是按未暴露处理。
const OUTPUT_SAFETY_EXPOSURE_LABELS = Object.freeze({
  none: "未暴露",
  partial: "部分已发送",
  complete: "完整已发送",
  unknown: "未知",
});
// 规则动作标签仅用于解释冻结命中，不授予用户编辑 DLP 规则或执行冲正的权限。
const OUTPUT_SAFETY_ACTION_LABELS = Object.freeze({
  allow: "允许",
  log_only: "仅记录",
  block: "阻断",
});

/**
 * 渲染 PAGE-100 输出安全证据。
 *
 * 参数：Control API 返回的严格 output_safety DTO。返回值：安全 HTML 或空字符串。
 * 安全约束：这里只消费稳定 ID、枚举、版本和非负计数；未知扩展字段、pattern、Prompt 和 Response 永不读取。
 */
function renderOutputSafetyEvidence(outputSafety) {
  if (!outputSafety || typeof outputSafety !== "object" || Array.isArray(outputSafety)) return "";
  const status = String(outputSafety.scan_status ?? "");
  const decision = String(outputSafety.decision ?? "");
  const blockedWarning = decision === "blocked"
    ? '<p class="output-safety-warning">已中断；此前已发送内容不可撤回。</p>'
    : "";
  const dispute = outputSafety.dispute && typeof outputSafety.dispute === "object"
    ? outputSafety.dispute
    : null;
  const reviewLabel = outputSafety.billing_disposition === "dispute_pending"
    ? `待争议${dispute?.reversal_eligible === true ? " / 可冲正" : ""}`
    : "无需复核";
  return `
    <section class="request-log-subsection output-safety-evidence" data-output-safety-status="${escapeAttribute(status)}">
      <div class="output-safety-evidence-heading">
        <h4>输出安全</h4>
        <span>${escapeHtml(OUTPUT_SAFETY_SCAN_STATUS_LABELS[status] ?? (status || "未知"))}</span>
      </div>
      ${blockedWarning}
      <dl class="request-log-detail-grid">
        ${renderPair("状态", OUTPUT_SAFETY_SCAN_STATUS_LABELS[status] ?? (status || "未知"))}
        ${renderPair("判定", OUTPUT_SAFETY_DECISION_LABELS[decision] ?? (decision || "未知"))}
        ${renderPair("动作", OUTPUT_SAFETY_ACTION_LABELS[outputSafety.action] ?? outputSafety.action ?? "-")}
        ${renderPair("暴露状态", OUTPUT_SAFETY_EXPOSURE_LABELS[outputSafety.exposure_state] ?? outputSafety.exposure_state ?? "未知")}
        ${renderPair("Provider 已观察 Token", outputSafety.observed_output_token_count ?? "未提供")}
        ${renderPair("已扫描 Token", outputSafety.scanned_token_count ?? "未提供")}
        ${renderPair("账单复核", reviewLabel)}
      </dl>
    </section>
  `;
}

/**
 * 渲染 PAGE-097 的不可变语义风险摘要。
 * 只消费后端独立 intent_risk DTO；缺少终态的 queued 请求必须保持 pending/best-effort，不能推断为 clean。
 */
function renderIntentRisk(intentRisk) {
  if (!intentRisk || typeof intentRisk !== "object" || Array.isArray(intentRisk)) return "";
  const status = String(intentRisk.status ?? "");
  return `
    <section class="request-log-subsection intent-risk-section" data-intent-risk-status="${escapeAttribute(status)}">
      <div class="intent-risk-heading">
        <h4>语义风险</h4>
        <span>${escapeHtml(intentEnumLabel(INTENT_STATUS_LABELS, status))}</span>
      </div>
      <p class="intent-risk-guidance">${escapeHtml(intentRiskGuidance(status))}</p>
      <dl class="request-log-detail-grid">
        ${renderPair("状态", intentEnumLabel(INTENT_STATUS_LABELS, status))}
        ${renderPair("风险类型", intentEnumLabel(INTENT_RISK_LABELS, intentRisk.risk_type))}
        ${renderPair("处理结果", intentEnumLabel(INTENT_ACTION_LABELS, intentRisk.action))}
      </dl>
    </section>
  `;
}

/** 固定解释各终态边界，明确旁路 detected、同步 blocked 和无终态 pending 的差异。 */
function intentRiskGuidance(status) {
  const guidance = {
    disabled: "历史记录或本次请求未启用语义分类。",
    pending: "任务仅进入进程内 best-effort 队列，当前尚无持久终态；可显式刷新详情。",
    clean: "当次冻结规则未检出风险，不代表未来规则版本的重算结论。",
    detected: "旁路观察发现风险，需要安全复核；模型请求未因此被阻断。",
    blocked: "同步强规则已在 Provider 调用前阻断，并已持久化安全结果。",
    failed: "分类 worker 已失败并保存固定失败终态，不展示底层异常正文。",
    skipped: "任务未进入分类队列；该状态不等于输入安全。",
  };
  return guidance[status] ?? "语义风险状态与当前前端合同不一致。";
}

/** 把语义风险固定枚举转换为产品文案；未知值保留原枚举并由调用方统一转义。 */
function intentEnumLabel(labels, value) {
  const normalized = value === null || value === undefined ? "" : String(value);
  return labels[normalized] ?? (normalized || "-");
}

/**
 * 渲染详情顶部摘要。Virtual Key 只显示 key_prefix，不显示完整密钥。
 */
function renderDetailSummary(log, detail = null) {
  const attempts = Array.isArray(detail?.routing_attempts) ? detail.routing_attempts : [];
  const routeSummary = routingSummaryLabel(attempts, detail);
  return `
    <dl class="request-log-detail-grid">
      ${renderPair("状态", STATUS_LABELS[log.status] ?? log.status)}
      ${renderPair("请求模型", log.request_model ?? "-")}
      ${renderPair("解析模型", publicModelLabel(log.resolved_model))}
      ${renderPair("模型服务", providerDisplayName(log))}
      ${renderPair("路由结果", routeSummary)}
      ${renderPair("延迟", formatLatency(log.latency_ms))}
      ${renderPair("成本估算", formatRequestCost(log, detail))}
      ${renderPair("安全检查", dlpSummaryLabel(log))}
    </dl>
  `;
}

/** 详情摘要按真实用量判断成本；价格已配置但上游没有 Token 时仍显示未计价。 */
function formatRequestCost(log, detail = null) {
  const usage = Array.isArray(detail?.usage) ? detail.usage : [];
  const hasUsage = usage.some((item) => (
    item?.usage_status === "recorded"
      || Number(item?.input_tokens) > 0
      || Number(item?.output_tokens) > 0
      || Number(item?.total_tokens) > 0
      || Number(item?.cache_tokens) > 0
  ));
  return hasUsage
    ? formatCost(log?.estimated_cost_amount, log?.currency, log?.price_schema_version)
    : "无 Token 用量";
}

/** 失败详情使用诊断结论；缺少归因时从已记录的尝试状态给出可操作的中文说明。 */
function renderFriendlyFailure(log, detail) {
  if (String(log?.status ?? "").toLowerCase() !== "failed") return "";
  const diagnosis = detail?.error_diagnosis;
  const attempts = Array.isArray(detail?.routing_attempts) ? detail.routing_attempts : [];
  const values = [
    diagnosis?.reason,
    diagnosis?.error_category,
    diagnosis?.error_code,
    ...attempts.map((item) => `${item?.error_category ?? ""} ${item?.error_code ?? ""} ${item?.trigger_error_code ?? ""} ${item?.status ?? ""}`),
  ].join(" ").toLowerCase();
  let reason = diagnosis?.reason;
  let suggestion = diagnosis?.suggestion;
  if (!reason || diagnosis?.rule_id === "unknown_error" || diagnosis?.matched_by === "unknown") {
    if (/quota|insufficient|billing|balance|credit/.test(values)) {
      reason = "模型服务额度不足或账单受限。";
      suggestion = "请检查模型服务账户余额、账单和额度。";
    } else if (/auth|unauthorized|forbidden|invalid_api_key|permission/.test(values)) {
      reason = "模型服务密钥无效或没有权限。";
      suggestion = "请检查模型服务的 API Key 和权限。";
    } else if (/timeout|timed.out/.test(values)) {
      reason = "模型服务响应超时。";
      suggestion = "请稍后重试；如果仍失败，检查模型服务地址和网络。";
    } else if (/rate.limit|too.many/.test(values)) {
      reason = "模型服务当前限流。";
      suggestion = "请稍后重试，或等待模型服务恢复。";
    } else if (/invalid_request|validation|bad.request|parameter/.test(values)) {
      reason = "请求参数不符合模型服务要求。";
      suggestion = "请检查模型名称和请求参数。";
    } else if (/stream_session_unavailable/.test(values)) {
      reason = "网关无法建立流式连接，该候选模型没有收到请求。";
      suggestion = "系统会继续尝试下一个可用模型；如果所有候选都失败，请检查网关状态。";
    } else if (/upstream_transport_failed/.test(values)) {
      reason = "模型服务网络连接失败。";
      suggestion = "系统已继续尝试下一个候选；请检查模型服务地址和网络。";
    } else if (/routing_decision_unavailable|routing_snapshot_unavailable/.test(values)) {
      reason = "系统没有取得路由快照，因此没有可靠的模型选择记录。";
      suggestion = "检查路由配置后重新发起调用。";
    } else if (/routing_attempt_unavailable/.test(values)) {
      reason = "系统没有保存模型调用尝试，无法判断模型端结果。";
      suggestion = "稍后重试；如果仍然发生，请检查网关日志。";
    } else if (/unavailable|network|connection|upstream|provider|failed/.test(values)) {
      reason = "模型服务暂时不可用。";
      suggestion = "请检查模型服务配置和网络后重试。";
    } else {
      reason = "这次调用未能完成，暂时没有足够信息判断具体原因。";
      suggestion = "请检查模型服务配置、API Key 和额度后重试。";
    }
  }
  return `
    <section class="request-log-subsection diagnosis-card">
      <h4>失败说明</h4>
      <dl class="request-log-detail-grid">
        ${renderPair("原因", reason)}
        ${renderPair("建议", suggestion)}
        ${renderPair("后续处理", attempts.length > 1 ? `已尝试 ${attempts.length} 个模型` : "没有可用的备用模型")}
      </dl>
    </section>
  `;
}

/** 说明是否已遍历全部候选，避免“尝试两次后停止”在详情里看起来像无故中断。 */
function routingSummaryLabel(attempts, detail = null) {
  if (!attempts.length) return "未进入模型调用";
  const succeeded = attempts.some((item) => ["succeeded", "provider_succeeded", "completed"].includes(item?.status));
  if (succeeded) {
    return attempts.length > 1 ? `${attempts.length} 个模型，已切换成功` : "首次调用成功";
  }
  if (attempts.length === 1 && !Array.isArray(detail?.routing_candidates)) {
    return "首次调用失败";
  }
  const candidateCount = Array.isArray(detail?.routing_candidates) ? detail.routing_candidates.length : attempts.length;
  if (attempts.length < candidateCount) {
    const last = attempts.at(-1);
    if (last?.error_code === "gateway.stream_session_unavailable" || last?.trigger_error_code === "gateway.stream_session_unavailable") {
      return `${attempts.length} 个模型已记录，剩余候选未执行`;
    }
    return last?.retryable === false ? `${attempts.length} 个模型失败，已停止` : `${attempts.length} 个模型失败，仍有备用模型`;
  }
  return `${attempts.length} 个模型，全部失败`;
}

/**
 * 渲染 DLP 命中摘要和命中明细。
 * DLP 信息只包含规则 ID、动作、命中哈希和长度，不展示用户输入、Prompt 或响应正文。
 */
function renderDlpDecision(log, matches) {
  if (!log?.dlp_status && !matches.length) {
    return "";
  }
  return `
    <section class="request-log-subsection dlp-decision-card">
      <h4>内容安全检查</h4>
      <dl class="request-log-detail-grid">
        ${renderPair("状态", dlpStatusLabel(log.dlp_status))}
        ${renderPair("动作", dlpActionLabel(log.dlp_action))}
        ${renderPair("命中数", log.dlp_match_count ?? matches.length)}
      </dl>
    </section>
  `;
}

/**
 * 渲染预算命中摘要和评估明细。
 * 预算信息只包含规则、scope、金额和动作，不展示 Prompt、响应正文或任何密钥明文。
 */
function renderBudgetDecision(log, evaluations) {
  if (!log?.budget_status && !evaluations.length) {
    return "";
  }
  return `
    <section class="request-log-subsection budget-decision-card">
      <h4>预算检查</h4>
      ${log.budget_status === "exhausted" && log.budget_action === "allow"
        ? '<p>预算已耗尽，当前设置为“仅告警”，本次调用仍被放行。失败原因请查看下方“失败说明”。</p>' : ""}
      <dl class="request-log-detail-grid">
        ${renderPair("状态", budgetStatusLabel(log.budget_status))}
        ${renderPair("处理结果", budgetActionLabel(log.budget_action))}
        ${renderPair("命中成本", formatCost(log.budget_current_cost_cny ?? 0, "CNY", "ledger-v1"))}
        ${renderPair("预算上限", formatCost(log.budget_limit_cny ?? 0, "CNY", "ledger-v1"))}
      </dl>
    </section>
  `;
}

/**
 * 渲染模型选择摘要，帮助管理员了解本次调用使用了哪个模型。
 */
function renderRoutingDecision(decision, attempts = []) {
  if (!decision) {
    return attempts.length
      ? `<div class="empty-state">已记录实际调用过程。</div>`
      : `<div class="empty-state">这次请求未进入模型服务。</div>`;
  }
  return `
    <section class="request-log-subsection">
      <h4>模型选择</h4>
      <dl class="request-log-detail-grid">
        ${renderPair("状态", routingCandidateEligibilityLabel(decision))}
        ${renderPair("已选模型", publicModelLabel(decision.selected_model_display_name))}
        ${renderPair("模型服务", providerDisplayName(decision))}
      </dl>
    </section>
  `;
}

/** 用 typed eligible/status 解释候选资格，未知状态保持原枚举而不推断为可调用。 */
function routingCandidateEligibilityLabel(item) {
  const status = item?.status ?? item?.decision_status;
  if (item?.eligible === true) return status === "selected" ? "已选" : "合格";
  if (item?.eligible === false) return "已剔除";
  const labels = { selected: "已选", standby: "备用", rejected: "已剔除" };
  return labels[status] ?? item?.eligibility ?? status ?? "未知";
}

/**
 * 渲染调用实际使用的访问策略结果，只保留用户可理解的状态和动作。
 */
function renderPolicySnapshotDecision(log) {
  return `
    <section class="request-log-subsection policy-snapshot-decision-card">
      <h4>访问策略</h4>
      <dl class="request-log-detail-grid">
        ${renderPair("状态", policySnapshotStatusLabel(log.policy_snapshot_status))}
        ${renderPair("决策", policyDecisionLabel(log.policy_decision))}
        ${renderPair("动作", policyActionLabel(log.policy_action_type))}
      </dl>
    </section>
  `;
}

/**
 * 格式化快照版本；NULL 根据状态显示未发布基线、未启用或不可用，绝不伪造 v0。
 */
function snapshotVersionLabel(log) {
  if (log?.snapshot_version !== null && log?.snapshot_version !== undefined) {
    return `v${log.snapshot_version}`;
  }
  if (log?.policy_snapshot_status === "disabled") {
    return "策略快照未启用";
  }
  if (log?.policy_snapshot_status === "unavailable") {
    return "快照不可用";
  }
  return "未发布基线";
}

/** 把 Gateway 快照租约状态转换为可读中文，原枚举仍可由后端日志精确检索。 */
function policySnapshotStatusLabel(value) {
  const labels = {
    disabled: "未启用",
    not_published: "尚未发布",
    current: "当前版本",
    lkg: "最近可用版本",
    emergency_lkg: "应急最近可用版本",
    expired: "已过期",
    unavailable: "不可用",
  };
  return labels[value] ?? value ?? "-";
}

/** 把 Policy 最终决策转换为中文；无命中保持短横线，不推断为 allow。 */
function policyDecisionLabel(value) {
  const labels = {
    allow: "放行",
    deny: "阻断",
    route_to: "重路由",
  };
  return labels[value] ?? value ?? "-";
}

/** 把 Policy 动作转换为中文，不把未知动作静默当作放行。 */
function policyActionLabel(value) {
  const labels = {
    allow: "放行",
    deny: "阻断",
    mask: "掩码",
    log_only: "仅记录",
    route_to: "重路由",
  };
  return labels[value] ?? value ?? "-";
}

/**
 * 按上游实际调用顺序渲染主备时间线。
 *
 * 失败尝试没有可信 usage 时明确显示“不可得”，预算准入估算不会冒充正式账单。
 */
function renderRoutingAttempts(attempts, detail = null, { compact = false } = {}) {
  if (!attempts.length) {
    return "";
  }
  const candidateCount = Array.isArray(detail?.routing_candidates) && detail.routing_candidates.length
    ? detail.routing_candidates.length
    : attempts.length;
  const providerNames = new Map();
  for (const candidate of Array.isArray(detail?.routing_candidates) ? detail.routing_candidates : []) {
    const name = String(candidate?.provider_name ?? "").trim();
    if (!name) continue;
    if (candidate?.provider_id) providerNames.set(String(candidate.provider_id), name);
    if (candidate?.provider_code) providerNames.set(String(candidate.provider_code).toLowerCase(), name);
  }
  return `
    <section class="request-log-subsection routing-attempt-section">
      <h4>模型服务尝试</h4>
      <p class="request-log-subsection-note">已尝试 ${escapeHtml(attempts.length)} / ${escapeHtml(candidateCount)} 个候选模型。</p>
      <div class="routing-attempt-list">
         ${attempts.map((item) => renderRoutingAttempt(item, detail, providerNames, { compact })).join("")}
      </div>
    </section>
  `;
}

/** 渲染单次上游尝试，只展示后端白名单字段。 */
function renderRoutingAttempt(item, detail = null, providerNames = null, { compact = false } = {}) {
  // primary 表示第一次调用而非 Alias default 角色；动态模式可能初始选中 fallback 配置候选。
  const role = item.attempt_role === "fallback" ? "备用模型重试" : "初始调用";
  const status = routingAttemptStatusLabel(item.status);
  const usage = {
    recorded: "已记录",
    unavailable: "未返回用量",
    not_applicable: "未发出上游调用",
    pending: "待记录",
  }[item.usage_status] ?? item.usage_status;
  const providerName = providerDisplayName(item, detail, providerNames);
  const response = ["succeeded", "provider_succeeded", "completed"].includes(item.status)
    ? "已完成"
    : item.upstream_http_status
      ? `已响应（HTTP ${item.upstream_http_status}）`
      : "未收到响应";
  return `
    <article class="routing-attempt-item status-${escapeAttribute(item.status)}">
      <header>
        <strong>#${escapeHtml(item.attempt_sequence)} ${role}</strong>
        <mark class="status-mark status-${escapeAttribute(item.status)}">${escapeHtml(status)}</mark>
      </header>
      <dl class="request-log-detail-grid">
        ${renderPair("模型", publicModelLabel(item.resolved_model))}
        ${renderPair("模型服务", providerName)}
        ${renderPair("服务响应", response)}
        ${renderPair("耗时", formatLatency(item.latency_ms))}
        ${renderPair("用量", usage)}
        ${renderPair("费用预估", formatAttemptCost(item))}
      </dl>
      ${compact ? "" : renderProviderErrorFact(item.provider_error_fact, "attempt")}
    </article>
  `;
}

/** 没有模型返回用量时，0 只是预算占位，不能伪装成本已经核算完成。 */
function formatAttemptCost(item) {
  const hasUsage = item?.usage_status === "recorded"
    || Number(item?.input_tokens ?? 0) > 0
    || Number(item?.output_tokens ?? 0) > 0
    || Number(item?.total_tokens ?? 0) > 0
    || Number(item?.cache_tokens ?? 0) > 0;
  if (!hasUsage) return "无 Token 用量";
  const schema = String(item?.price_schema_version ?? "").trim().toLowerCase();
  // The ledger uses an explicit unpriced version and a zero amount as a
  // bookkeeping placeholder. Never present that placeholder as a real fee.
  if (!schema || schema.startsWith("unpriced")) return "价格未记录";
  return formatEstimatedCost(item?.estimated_cost_cny, "CNY");
}

/** 把路由尝试的全部后端终态收敛为用户可理解的状态，避免合法状态落入“未知”。 */
function routingAttemptStatusLabel(value) {
  return {
    started: "进行中",
    calling: "调用中",
    provider_calling: "调用中",
    succeeded: "成功",
    provider_succeeded: "成功",
    completed: "成功",
    failed: "失败",
    upstream_failed: "上游失败",
    timed_out: "已超时",
    cancelled: "已取消",
    client_disconnected: "客户端中断",
    output_blocked: "输出被阻断",
    output_safety_unavailable: "安全检查失败",
  }[value] ?? (value || "状态不可用");
}

/**
 * 把 Control API 的单个 ``stream_session`` owner 转成渲染数组。
 *
 * PRD-056 沿用既有 RequestLogDetail 单会话合同；复数字段只作为旧测试/代理兼容回退，
 * 不能为了前端布局虚构第二个 StreamSession 或把其 Fact 塞进 RoutingAttempt。
 */
function requestLogStreamSessions(detail) {
  if (detail?.stream_session && typeof detail.stream_session === "object") {
    // 新合同优先返回唯一可信 StreamSession，避免把同一流式失败重复挂载到多个 owner。
    return [detail.stream_session];
  }
  // 复数字段只用于旧响应兼容；缺失时返回空数组，不伪造任何会话或错误事实。
  return Array.isArray(detail?.stream_sessions) ? detail.stream_sessions : [];
}

/**
 * 渲染流式调用终态与其模型服务错误处理结果。
 * 流式失败由 StreamSession 自己持有 Fact，前端不能为展示方便伪造 RoutingAttempt 或合并两个 owner。
 */
function renderStreamSessions(sessions, detail = null) {
  const safeSessions = Array.isArray(sessions) ? sessions : [];
  const fallbackLog = detail?.log ?? detail;
  // 没有可信会话时不生成占位 DOM，避免用户误以为请求曾进入流式 Provider。
  if (!safeSessions.length) {
    return "";
  }
  return `
    <section class="request-log-subsection stream-session-section">
       <h4>流式传输</h4>
      <div class="stream-session-list">
        ${safeSessions.map((item) => `
          <article class="stream-session-item status-${escapeAttribute(item?.status ?? "unknown")}">
             <header><strong>流式传输</strong><mark class="status-mark status-${escapeAttribute(item?.status ?? "unknown")}">${escapeHtml(streamSessionStatusLabel(item?.status))}</mark></header>
            <dl class="request-log-detail-grid">
              ${renderPair("模型", publicModelLabel(item?.resolved_model ?? fallbackLog?.resolved_model))}
              ${renderPair("模型服务", providerDisplayName(item, fallbackLog))}
              ${renderPair("可重试", booleanLabel(item?.retryable))}
              ${renderPair("结束时间", formatDateTime(item?.completed_at))}
            </dl>
            ${renderProviderErrorFact(item?.provider_error_fact, "stream_session")}
          </article>
        `).join("")}
      </div>
    </section>
  `;
}

/**
 * 渲染冻结的模型服务错误处理结果。
 * 只消费后端 DTO 的枚举、ID、hash、存在位和长度桶；原始 code/type/message/body 即使意外出现
 * 也不会被模板读取，避免日志详情成为上游响应泄漏通道。
 */
function renderProviderErrorFact(fact, ownerKind) {
  if (!fact || typeof fact !== "object" || Array.isArray(fact)) {
    return "";
  }
  return `
    <section class="provider-error-fact" data-provider-error-fact-owner="${escapeAttribute(ownerKind)}">
      <h5>模型服务结果</h5>
      <dl class="request-log-detail-grid">
        ${renderPair("处理结果", fact.normalized_output_error_code ? "已统一处理" : "未统一处理")}
        ${renderPair("可重试", booleanLabel(fact.retryable))}
        ${renderPair("连接状态", fact.upstream_http_status ? "服务已响应" : "未收到响应")}
      </dl>
    </section>
  `;
}

/**
 * 渲染归一化规则引用。
 * tenant 必须同时有稳定资源 ID 和正版本；builtin 必须同时有安全规则 ID 和不可变目录版本；
 * guard、损坏 DTO 或缺失版本只显示纯文本。规则管理已收口到 Provider 详情，调用日志不再生成顶级页面跳转。
 */
function renderProviderErrorRuleReference(source, ruleId, fact) {
  const normalizedRuleId = String(ruleId ?? "");
  if (
    source === "tenant"
    && TENANT_ERROR_MAP_REFERENCE_PATTERN.test(normalizedRuleId)
    && Number.isSafeInteger(fact?.matched_rule_version)
    && fact.matched_rule_version > 0
  ) {
    return `<div><dt>命中规则</dt><dd><code>${escapeHtml(normalizedRuleId)}</code> · v${escapeHtml(fact.matched_rule_version)}</dd></div>`;
  }
  const catalogVersion = String(fact?.builtin_catalog_version ?? "");
  if (
    source === "builtin"
    && SAFE_ERROR_MAP_REFERENCE_PATTERN.test(normalizedRuleId)
    && SAFE_ERROR_MAP_REFERENCE_PATTERN.test(catalogVersion)
  ) {
    return `<div><dt>命中规则</dt><dd><code>${escapeHtml(normalizedRuleId)}</code> · ${escapeHtml(catalogVersion)}</dd></div>`;
  }
  return renderPair(
    "命中规则",
    source === "guard" ? `${normalizedRuleId || "-"} · 不可覆盖 guard` : normalizedRuleId || "-",
  );
}

/** 三个存在位独立展示；缺失字段不能由 class 或 projection 反向猜测。 */
function providerErrorPresenceLabel(fact) {
  return [
    `code:${fact.provider_error_code_present === true ? "有" : "无"}`,
    `type:${fact.provider_error_type_present === true ? "有" : "无"}`,
    `message:${fact.message_present === true ? "有" : "无"}`,
  ].join(" / ");
}

/** 规则来源转为固定产品文案，未知值保留原枚举便于发现 DTO 漂移。 */
function providerErrorSourceLabel(value) {
  return { tenant: "租户覆盖", builtin: "平台内置", guard: "安全 guard" }[value] ?? value ?? "-";
}

/** 三态布尔展示：缺失不能伪装为 false。 */
function booleanLabel(value) {
  return value === true ? "是" : value === false ? "否" : "-";
}

/** StreamSession 状态只做显示转换，不从 HTTP 或错误码重新推导终态。 */
function streamSessionStatusLabel(value) {
  return {
    starting: "准备中",
    active: "传输中",
    started: "进行中",
    streaming: "传输中",
    succeeded: "成功",
    completed: "已完成",
    failed: "失败",
    upstream_failed: "上游失败",
    timed_out: "已超时",
    client_disconnected: "客户端中断",
    output_blocked: "输出被阻断",
    output_safety_unavailable: "安全检查不可用",
    cancelled: "已取消",
  }[value] ?? value ?? "-";
}

/**
 * 渲染调用日志详情里的失败说明。
 *
 * 成功请求没有诊断字段；失败请求只展示后端派生的安全原因和操作建议，不展示上游原始正文。
 */
function renderDiagnosisCard(diagnosis) {
  if (!diagnosis) {
    return "";
  }
  const isUnknown = diagnosis.rule_id === "unknown_error" || diagnosis.matched_by === "unknown";
  const retryableLabel = diagnosis.retryable ? "可以重试" : "不建议直接重试";
  const reason = isUnknown ? "暂时无法判断具体原因。" : diagnosis.reason;
  const suggestion = isUnknown ? "请检查模型服务配置、API Key 和额度；仍失败时联系管理员。" : diagnosis.suggestion;
  return `
    <section class="request-log-subsection diagnosis-card">
      <h4>失败说明</h4>
      <dl class="request-log-detail-grid">
        ${renderPair("原因", reason)}
        ${renderPair("建议", suggestion)}
        ${renderPair("是否可重试", retryableLabel)}
      </dl>
    </section>
  `;
}

/**
 * 渲染用量和成本条目。normalized_usage 只包含 token/成本摘要，不含 Prompt 明文。
 */
function renderUsage(usage) {
  if (!usage.length) {
    return `<div class="empty-state">这次调用没有产生可计费用量。</div>`;
  }
  return `
    <section class="request-log-subsection">
      <h4>用量与成本</h4>
      ${usage
        .map(
          (item) => `
            <dl class="request-log-detail-grid">
              ${renderPair("输入 Token", item.input_tokens)}
              ${renderPair("输出 Token", item.output_tokens)}
              ${renderPair("总 Token", item.total_tokens)}
              ${renderPair("缓存令牌", item.cache_tokens ?? "未返回")}
              ${renderPair("成本", formatCost(item.cost_amount, item.currency, item.price_schema_version))}
              ${renderPair("成本 CNY", formatCost(
                item.cost_cny ?? (item.currency === "CNY" ? item.cost_amount : null),
                "CNY",
                item.price_schema_version,
              ))}
              ${renderPair("成本 USD", formatCost(
                item.cost_usd ?? (item.currency === "USD" ? item.cost_amount : null),
                "USD",
                item.price_schema_version,
              ))}
              ${renderPair("用量来源", item.estimated ? "系统估算" : "模型服务返回")}
            </dl>
          `,
        )
        .join("")}
    </section>
  `;
}

/**
 * 渲染一个详情键值对。统一函数可以保证所有值都经过 HTML 转义。
 */
function renderPair(label, value) {
  return `
    <dt>${escapeHtml(label)}</dt>
    <dd>${escapeHtml(value ?? "-")}</dd>
  `;
}

/**
 * 转换预算状态标签，未命中时保持短横线。
 */
function budgetStatusLabel(value) {
  const labels = {
    threshold_reached: "达到告警",
    exhausted: "已耗尽",
  };
  return labels[value] ?? value ?? "-";
}

/**
 * 转换预算动作标签，避免直接暴露内部枚举影响可读性。
 */
function budgetActionLabel(value) {
  const labels = {
    warn: "告警",
    allow: "放行",
    block: "阻断",
  };
  return labels[value] ?? value ?? "-";
}

/**
 * 转换 DLP 状态标签，未命中时保持短横线，命中时突出阻断或试运行结果。
 */
function dlpStatusLabel(value) {
  const labels = {
    clean: "未命中",
    matched: "已命中",
    blocked: "已阻断",
  };
  return labels[value] ?? value ?? "-";
}

/**
 * 转换 DLP 动作标签，log_only 明确显示为试运行，避免管理员误解为未生效。
 */
function dlpActionLabel(value) {
  const labels = {
    allow: "放行",
    log_only: "试运行记录",
    block: "阻断",
  };
  return labels[value] ?? value ?? "-";
}

/**
 * 生成日志详情顶部的 DLP 简短摘要，便于从基础元数据快速判断是否触发安全策略。
 */
function dlpSummaryLabel(log) {
  if (!log?.dlp_status) {
    return "-";
  }
  const action = dlpActionLabel(log.dlp_action);
  const count = log.dlp_match_count ?? 0;
  return `${dlpStatusLabel(log.dlp_status)} / ${action} / ${count} 次`;
}

/**
 * 渲染 select option，并保持当前过滤条件选中态。
 */
function renderOption(value, label, selectedValue) {
  const selected = value === (selectedValue ?? "") ? " selected" : "";
  return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
}

/**
 * 格式化毫秒延迟。空值说明请求还没写入延迟或写入失败。
 */
function formatLatency(value) {
  if (value === null || value === undefined) {
    return "-";
  }
  return `${value} ms`;
}

/**
 * 格式化 token 和成本，便于表格中一眼扫描。
 */
function formatUsageAndCost(item) {
  const hasUsage = Number(item?.total_tokens) > 0
    || Number(item?.input_tokens) > 0
    || Number(item?.output_tokens) > 0;
  const usage = hasUsage ? `${item.total_tokens} tokens` : "用量未返回";
  const cost = hasUsage
    ? formatCost(item.estimated_cost_amount, item.currency, item.price_schema_version)
    : "价格未记录";
  return `${usage} / ${cost}`;
}

/**
 * 格式化成本估算，金额来自后端按模型价格快照计算的结果。
 */
function formatCost(amount, currency, priceSchemaVersion = "") {
  const schema = String(priceSchemaVersion ?? "").trim().toLowerCase();
  if (!schema || schema.startsWith("unpriced") || amount === null || amount === undefined || amount === "") {
    return "价格未记录";
  }
  const value = Number(amount).toFixed(6).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
  return `${value} ${currency ?? "CNY"}`;
}

function formatEstimatedCost(amount, currency = "CNY") {
  if (amount === null || amount === undefined || amount === "") {
    return "无可计费用量";
  }
  const value = Number(amount);
  if (!Number.isFinite(value)) {
    return "无可计费用量";
  }
  return `${value.toFixed(6).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1")} ${currency}`;
}

function providerDisplayName(item, fallback = null, providerNames = null) {
  const fallbackLog = fallback?.log ?? fallback;
  const name = String(item?.provider_name ?? "").trim();
  if (name) return name;
  const providerId = String(item?.provider_id ?? "").trim();
  const providerCode = String(item?.provider_code ?? "").trim().toLowerCase();
  if (providerNames instanceof Map) {
    const mapped = providerNames.get(providerId) ?? providerNames.get(providerCode);
    if (mapped) return mapped;
  }
  const fallbackName = String(fallbackLog?.provider_name ?? "").trim();
  if (fallbackName) return fallbackName;
  const code = providerCode;
  const fallbackCode = String(fallbackLog?.provider_code ?? "").trim().toLowerCase();
  if (!code && fallbackCode) return PROVIDER_LABELS[fallbackCode] ?? fallbackCode;
  // Provider code is an internal routing key; never expose it when the
  // tenant-facing name is missing.
  return PROVIDER_LABELS[code] ?? "自定义模型服务";
}

function publicModelLabel(value) {
  const text = String(value ?? "").trim();
  if (!text || /^(?:mdl|model|provider|prv|route|rte)_[a-z0-9_-]{8,}$/i.test(text)) return "未识别模型";
  return text;
}

function ownerDisplayName(item) {
  const value = String(item?.consumer_name ?? item?.key_owner_name ?? item?.virtual_key_name ?? "").trim();
  return value || "未标记使用者";
}

function scopeDisplayName(item) {
  const project = String(item?.project_name ?? "").trim();
  const application = String(item?.application_name ?? "").trim();
  if (project && application) return `${project} / ${application}`;
  return project || application || "未绑定项目";
}

/**
 * 格式化 ISO 时间字符串，避免表格中出现过长的小数秒。
 */
function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23", timeZoneName: "shortOffset",
  }).format(date);
}

/**
 * 渲染操作级错误提示，例如详情读取失败。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="request-log-action-error" role="alert">
      ${escapeHtml(error.message ?? "调用日志操作失败")}
    </div>
  `;
}

/**
 * HTML 文本转义，防止日志字段中的特殊字符破坏页面结构。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * HTML 属性转义，额外处理反引号以保护 data-* 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
