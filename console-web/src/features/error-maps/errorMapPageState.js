/**
 * 模块职责：维护 PAGE-081 的租户身份 owner、列表刷新、单层编辑对话框和冲突重取生命周期。
 * 安全边界：状态只保存公开规则 DTO 与用户草稿；Bearer Token、tenant_id 表单值、原始上游错误
 * 和 AbortController 均不会进入可渲染快照。
 */

import { createErrorMapRequestId } from "../../lib/errorMaps.js";
import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";

// 日志跳转只接受稳定的公开标识；tenant 资源 ID 使用后端冻结格式，builtin ID/目录版本使用安全 ASCII。
const ERROR_MAP_NAVIGATION = "error-maps";
const TENANT_ERROR_MAP_ID_PATTERN = /^pem_[0-9a-f]{32}$/;
const SAFE_ERROR_MAP_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;

/**
 * 冻结错误规则页面的完整身份 owner。
 * tenant、用户、角色、Session ID 或 revision 任一变化都必须淘汰旧列表、草稿与迟到回调。
 */
export function errorMapIdentitySnapshot(authContext) {
  // 错误归一 owner 必须绑定读权限、矩阵 revision 和指纹，降权后旧草稿立即失效。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PROVIDER_ERROR_MAP_READ);
}

/**
 * 创建 PAGE-082 到 PAGE-081 的一次性内存交接仓库。
 *
 * 交接项同时绑定完整认证身份与 tenant owner；规则 ID、版本和目录版本只允许公开安全格式，
 * 避免把 request_id、pattern、原始 Provider 摘要或任意 DOM 文本带入目标页或持久存储。
 */
export function createErrorMapHandoff() {
  let entry = null;
  let generation = 0;

  return {
    /** 写入一次跳转；任一 owner 或规则身份字段非法都会先销毁旧项并返回 false。 */
    stage({
      identity,
      tenantId,
      source,
      errorMapId,
      matchedRuleVersion,
      builtinCatalogVersion,
    }) {
      const normalizedIdentity = text(identity);
      const normalizedTenantId = normalizeSafeIdentifier(tenantId);
      const reference = normalizeErrorMapReference({
        source,
        errorMapId,
        requestedVersion: matchedRuleVersion,
        requestedCatalogVersion: builtinCatalogVersion,
      });
      generation += 1;
      entry = normalizedIdentity && normalizedTenantId && reference
        ? Object.freeze({
            identity: normalizedIdentity,
            tenantId: normalizedTenantId,
            ...reference,
            generation,
          })
        : null;
      return Boolean(entry);
    },

    /** 仅目标导航和完全相同的身份、租户可以消费；无论匹配与否都立即销毁，防止跨页重放。 */
    consume({ identity, tenantId, navigation }) {
      const current = entry;
      entry = null;
      if (
        !current
        || current.identity !== text(identity)
        || current.tenantId !== normalizeSafeIdentifier(tenantId)
        || text(navigation) !== ERROR_MAP_NAVIGATION
      ) {
        return null;
      }
      return copyReference(current);
    },

    /** 登出、离页、降权、401、pagehide 或导航失败都使用同一同步销毁入口。 */
    clear() {
      generation += 1;
      entry = null;
    },

    /** 测试快照只暴露代次和 pending 位，不形成规则 ID 或版本的第二条读取通道。 */
    snapshot() {
      return { generation, pending: Boolean(entry) };
    },
  };
}

/** 创建错误规则页面纯内存仓库；读取、写入和冲突重取使用互不取消的 ticket。 */
export function createErrorMapPageMemory({ requestIdFactory } = {}) {
  const createRequestId = requestIdFactory ?? createErrorMapRequestId;
  let activeIdentity = "";
  let activeNavigation = "";
  let activeQuery = "";
  let refreshGeneration = 0;
  let refreshController = null;
  let operationGeneration = 0;
  let operationTicket = null;
  let conflictGeneration = 0;
  let conflictController = null;
  let retryOperation = null;
  let rules = [];
  let providers = [];
  let head = emptyHead();
  let pageMeta = { total: 0, limit: 100, offset: 0 };
  let refreshError = null;
  let dialog = null;
  // 已消费的日志引用可以在目标页周期刷新间保留，但 owner/query 变化会随其他页面状态一起销毁。
  let reference = null;

  /** owner 或筛选变化先同步 abort；相同 owner 的后台 GET 不应取消正在提交的 mutation。 */
  function enter(identity, navigation, query = activeQuery) {
    const nextIdentity = text(identity);
    const nextNavigation = text(navigation);
    const nextQuery = text(query);
    if (
      nextIdentity === activeIdentity
      && nextNavigation === activeNavigation
      && nextQuery === activeQuery
    ) {
      return;
    }
    invalidateAll();
    activeIdentity = nextIdentity;
    activeNavigation = nextNavigation;
    activeQuery = nextQuery;
  }

  /** 销毁三个异步 owner 和全部业务状态；abort 之外仍推进代次以防 fetch 忽略 signal。 */
  function invalidateAll() {
    refreshController?.abort();
    operationTicket?.controller.abort();
    conflictController?.abort();
    refreshController = null;
    operationTicket = null;
    conflictController = null;
    refreshGeneration += 1;
    operationGeneration += 1;
    conflictGeneration += 1;
    retryOperation = null;
    rules = [];
    providers = [];
    head = emptyHead();
    pageMeta = { total: 0, limit: 100, offset: 0 };
    refreshError = null;
    dialog = null;
    reference = null;
  }

  /** 核对 GET ticket 仍属于最新身份、导航、筛选与读取代次。 */
  function isCurrentRefresh(ticket, owner = {}) {
    return Boolean(
      ticket
      && !ticket.signal.aborted
      && ticket.generation === refreshGeneration
      && ticket.identity === activeIdentity
      && ticket.navigation === activeNavigation
      && ticket.query === activeQuery
      && ticket.identity === text(owner.identity ?? activeIdentity)
      && ticket.navigation === text(owner.navigation ?? activeNavigation),
    );
  }

  /** 核对 mutation 仍属于当前对话框、身份、导航和草稿代次。 */
  function isCurrentOperation(ticket, owner = {}) {
    return Boolean(
      ticket
      && dialog
      && operationTicket === ticket
      && !ticket.signal.aborted
      && ticket.generation === operationGeneration
      && ticket.identity === activeIdentity
      && ticket.navigation === activeNavigation
      && ticket.dialogRevision === dialog.revision
      && ticket.identity === text(owner.identity ?? activeIdentity)
      && ticket.navigation === text(owner.navigation ?? activeNavigation),
    );
  }

  /** 核对冲突 GET 仍对应当前发生 409 的同一稳定规则资源。 */
  function isCurrentConflict(ticket, owner = {}) {
    return Boolean(
      ticket
      && dialog?.conflicted
      && conflictController === ticket.controller
      && !ticket.signal.aborted
      && ticket.generation === conflictGeneration
      && ticket.errorMapId === dialog.errorMapId
      && ticket.identity === activeIdentity
      && ticket.navigation === activeNavigation
      && ticket.identity === text(owner.identity ?? activeIdentity)
      && ticket.navigation === text(owner.navigation ?? activeNavigation),
    );
  }

  return {
    /** 开始列表读取；新 GET 只淘汰旧 GET，绝不取消写请求或冲突重取。 */
    beginRefresh({ identity, navigation, query, reference: nextReference }) {
      const referenceProvided = nextReference !== undefined;
      const normalizedReference = referenceProvided
        ? normalizeErrorMapReference(nextReference)
        : null;
      enter(identity, navigation, query);
      if (referenceProvided) {
        // handoff 已在 consume 时完成 owner 校验；这里再次白名单复制，避免调用方随后修改同一对象。
        reference = normalizedReference;
      }
      refreshController?.abort();
      refreshController = new AbortController();
      const generation = ++refreshGeneration;
      return Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        query: activeQuery,
        generation,
        controller: refreshController,
        signal: refreshController.signal,
      });
    },

    /** 接纳严格 owner 下的 GET；公开 DTO 会被白名单复制，未知字段不会进入页面状态。 */
    acceptRefresh(ticket, payload, owner) {
      if (!isCurrentRefresh(ticket, owner)) {
        return false;
      }
      const rawRules = Array.isArray(payload?.error_maps)
        ? payload.error_maps
        : Array.isArray(payload?.rules) ? payload.rules : [];
      rules = rawRules.map(copyRule).filter((rule) => rule.error_map_id);
      const rawProviders = Array.isArray(payload?.provider_options)
        ? payload.provider_options
        : Array.isArray(payload?.providers) ? payload.providers : [];
      providers = rawProviders.map(copyProvider).filter((provider) => provider.provider_id);
      head = copyHead(payload?.head ?? payload);
      pageMeta = {
        total: nonnegativeInteger(payload?.total, rules.length),
        limit: positiveInteger(payload?.limit, 100),
        offset: nonnegativeInteger(payload?.offset, 0),
      };
      refreshError = null;
      if (refreshController === ticket.controller) {
        refreshController = null;
      }
      return true;
    },

    /** 首次 GET 失败进入错误态；已有列表保留并展示可追踪的后台刷新错误。 */
    acceptRefreshError(ticket, error, owner) {
      if (!isCurrentRefresh(ticket, owner)) {
        return false;
      }
      refreshError = safeError(error, "error_map.unavailable", "错误规则读取失败");
      if (refreshController === ticket.controller) {
        refreshController = null;
      }
      return true;
    },

    /** 打开新建对话框；草稿只含后端允许字段，默认不创建宽泛 message 规则。 */
    openCreate() {
      if (!activeIdentity || activeNavigation !== "error-maps" || operationTicket) {
        return false;
      }
      dialog = createDialog("create", null, null, emptyDraft());
      retryOperation = null;
      return true;
    },

    /** 打开租户规则编辑器；builtin/readonly 行即使伪造点击也会失败关闭。 */
    openEdit(errorMapId) {
      if (!activeIdentity || activeNavigation !== "error-maps" || operationTicket) {
        return false;
      }
      const rule = rules.find((item) => item.error_map_id === text(errorMapId));
      // 编辑器只接受当前列表中的可写租户规则，builtin、只读或无版本对象均失败关闭。
      if (!rule || rule.source !== "tenant" || rule.readonly || !validVersion(rule.version)) {
        return false;
      }
      dialog = createDialog("edit", rule.error_map_id, rule.version, draftFromRule(rule));
      retryOperation = null;
      return true;
    },

    /** 替换当前草稿并计算 dirty；冲突或 busy 时禁止修改，避免草稿与 ticket 指纹漂移。 */
    updateDraft(nextDraft) {
      if (!dialog || dialog.busy || dialog.conflicted) {
        return false;
      }
      dialog.draft = normalizeDraft(nextDraft);
      dialog.revision += 1;
      dialog.dirty = canonicalDraft(dialog.draft) !== canonicalDraft(dialog.authoritativeDraft);
      dialog.actionError = null;
      dialog.feedback = dialog.dirty ? "存在未保存修改" : "";
      if (!dialog.dirty) {
        retryOperation = null;
      }
      return true;
    },

    /** 为启停按钮准备与普通编辑相同的 CAS 草稿，后续仍由统一 acquireMutation 发 PATCH。 */
    prepareStatusChange(errorMapId, status) {
      if (!activeIdentity || activeNavigation !== "error-maps" || operationTicket) {
        return false;
      }
      const rule = rules.find((item) => item.error_map_id === text(errorMapId));
      // 启停同样绑定可写租户规则和冻结版本，不能绕过普通编辑入口的 owner 校验。
      if (!rule || rule.source !== "tenant" || rule.readonly || !validVersion(rule.version)) {
        return false;
      }
      dialog = createDialog("edit", rule.error_map_id, rule.version, draftFromRule(rule));
      retryOperation = null;
      const nextStatus = status === "active" ? "active" : "disabled";
      dialog.draft = normalizeDraft({ ...dialog.draft, status: nextStatus });
      dialog.revision += 1;
      dialog.dirty = canonicalDraft(dialog.draft) !== canonicalDraft(dialog.authoritativeDraft);
      dialog.feedback = dialog.dirty ? "存在未保存修改" : "";
      return dialog.dirty;
    },

    /** 取得唯一 mutation ticket；同一指纹仅在上次网络结果未知时复用 request_id。 */
    acquireMutation({ identity, navigation }) {
      enter(identity, navigation);
      if (!dialog || !dialog.dirty || dialog.conflicted) {
        return { state: "blocked", reason: dialog?.conflicted ? "conflict" : "not_dirty" };
      }
      if (operationTicket) {
        return { state: "ok", ticket: operationTicket, reused: true };
      }
      const fingerprint = JSON.stringify([
        dialog.mode,
        dialog.errorMapId,
        dialog.baseVersion,
        JSON.parse(canonicalDraft(dialog.draft)),
      ]);
      const requestId = retryOperation?.fingerprint === fingerprint
        ? retryOperation.requestId
        : createRequestId();
      const controller = new AbortController();
      const generation = ++operationGeneration;
      operationTicket = Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        generation,
        dialogRevision: dialog.revision,
        mode: dialog.mode,
        errorMapId: dialog.errorMapId,
        expectedVersion: dialog.baseVersion,
        draft: Object.freeze({ ...dialog.draft, http_statuses: [...dialog.draft.http_statuses] }),
        fingerprint,
        requestId,
        controller,
        signal: controller.signal,
      });
      dialog.busy = true;
      dialog.actionError = null;
      dialog.feedback = "保存中";
      return { state: "ok", ticket: operationTicket, reused: false };
    },

    /** 接纳 200/201/noop；响应必须包含同一资源和合法版本，随后关闭对话框。 */
    acceptMutationSuccess(ticket, payload, owner) {
      if (!isCurrentOperation(ticket, owner)) {
        return false;
      }
      const rule = copyRule(payload?.error_map ?? payload?.provider_error_map ?? payload?.rule);
      const changed = payload?.changed;
      const validResource = Boolean(
        rule.error_map_id
        && validVersion(rule.version)
        && (ticket.mode === "create" || rule.error_map_id === ticket.errorMapId)
        && (changed === false || ticket.mode === "create" || rule.version === ticket.expectedVersion + 1),
      );
      if (!validResource) {
        return false;
      }
      const index = rules.findIndex((item) => item.error_map_id === rule.error_map_id);
      if (index >= 0) {
        rules[index] = rule;
      } else {
        rules = [...rules, rule];
      }
      head = copyHead(payload?.head ?? payload, head);
      operationTicket = null;
      retryOperation = null;
      dialog = null;
      return true;
    },

    /**
     * 处理 mutation 失败：409 保留原草稿并等待独立 GET；HTTP 终态丢弃 ID，只有无 status 的
     * 网络结果未知失败保留相同指纹和 request_id。
     */
    acceptMutationFailure(ticket, error, owner) {
      if (!isCurrentOperation(ticket, owner)) {
        return false;
      }
      operationTicket = null;
      dialog.busy = false;
      dialog.actionError = safeError(error, "error_map.update_failed", "错误规则保存失败");
      // 只有带 HTTP 409 的编辑请求进入冲突态；网络结果未知必须保留原幂等键供安全重试。
      if (error?.status === 409 && ticket.mode === "edit") {
        dialog.conflicted = true;
        dialog.feedback = "规则版本已变化，请读取并显式载入最新版本";
        retryOperation = null;
      } else if (error?.status === undefined || error?.status === null) {
        dialog.feedback = "网络结果未知，再次保存将复用同一 request_id";
        retryOperation = { fingerprint: ticket.fingerprint, requestId: ticket.requestId };
      } else {
        dialog.feedback = "保存失败，请修正后重试";
        retryOperation = null;
      }
      return true;
    },

    /** 409 后创建独立精确 GET ticket；其 request_id 必须与失败 PATCH 和下一次 PATCH 都不同。 */
    beginConflictRefresh({ identity, navigation }) {
      if (
        !dialog?.conflicted
        || !dialog.errorMapId
        || identity !== activeIdentity
        || navigation !== activeNavigation
      ) {
        return null;
      }
      conflictController?.abort();
      conflictController = new AbortController();
      const generation = ++conflictGeneration;
      return Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        generation,
        errorMapId: dialog.errorMapId,
        requestId: createRequestId(),
        controller: conflictController,
        signal: conflictController.signal,
      });
    },

    /** 接纳冲突精确 GET；只保存同一 tenant-qualified ID 的单条最新规则，不覆盖用户草稿。 */
    acceptConflictRefresh(ticket, payload, owner) {
      if (!isCurrentConflict(ticket, owner)) {
        return false;
      }
      const candidates = Array.isArray(payload?.error_maps)
        ? payload.error_maps
        : Array.isArray(payload?.rules) ? payload.rules : [];
      const latest = candidates.length === 1 ? copyRule(candidates[0]) : {};
      if (
        latest.error_map_id !== ticket.errorMapId
        || latest.source !== "tenant"
        || !validVersion(latest.version)
      ) {
        return false;
      }
      dialog.sideLatest = latest;
      dialog.feedback = `已读取服务器 v${latest.version}，原草稿仍保留`;
      conflictController = null;
      return true;
    },

    /** 冲突 GET 的统一错误只更新当前 dialog，不结束认证或清理另一 owner。 */
    acceptConflictError(ticket, error, owner) {
      if (!isCurrentConflict(ticket, owner)) {
        return false;
      }
      dialog.actionError = safeError(error, "error_map.conflict_refresh_failed", "最新规则读取失败");
      dialog.feedback = "原草稿仍保留";
      conflictController = null;
      return true;
    },

    /** 用户显式载入最新服务器版本；旧草稿被替换，下一次编辑会使用新 expected_version 和新 ID。 */
    adoptConflict() {
      if (!dialog?.conflicted || dialog.busy || !dialog.sideLatest) {
        return false;
      }
      const latest = dialog.sideLatest;
      dialog.baseVersion = latest.version;
      dialog.authoritativeDraft = draftFromRule(latest);
      dialog.draft = draftFromRule(latest);
      dialog.revision += 1;
      dialog.dirty = false;
      dialog.conflicted = false;
      dialog.sideLatest = null;
      dialog.actionError = null;
      dialog.feedback = `已载入服务器 v${latest.version}，请重新修改后保存`;
      retryOperation = null;
      return true;
    },

    /** 非 busy 对话框可由取消、Escape 或离页关闭；关闭同时取消冲突 GET 和未知结果重试票据。 */
    closeDialog() {
      if (!dialog || dialog.busy) {
        return false;
      }
      conflictController?.abort();
      conflictController = null;
      conflictGeneration += 1;
      retryOperation = null;
      dialog = null;
      return true;
    },

    /** dialog、dirty、busy、conflict 任一存在时禁止同 owner 的全壳 DOM 替换。 */
    blocksShellDomReplacement() {
      return Boolean(dialog && (dialog.dirty || dialog.busy || dialog.conflicted || dialog.mode));
    },

    /** 返回不包含 Token、Session 明文、AbortController 或 mutation request_id 的渲染快照。 */
    snapshot() {
      return {
        state: refreshError && !rules.length ? "error" : "ok",
        rules: rules.map(copyRule),
        providers: providers.map(copyProvider),
        head: { ...head },
        refreshError: rules.length ? refreshError : refreshError,
        ...pageMeta,
        dialog: dialog ? copyDialog(dialog) : null,
        reference: reference ? copyReference(reference) : null,
      };
    },

    /** 离页、登出、降权、401、pagehide 或身份变化时同步销毁全部 owner 和草稿。 */
    leave() {
      invalidateAll();
      activeIdentity = "";
      activeNavigation = "";
      activeQuery = "";
    },

    isCurrentRefresh,
    isCurrentOperation,
    isCurrentConflict,
  };
}

/** 创建对话框内部状态；authoritativeDraft 用于精确 dirty 比较而不是依赖 DOM。 */
function createDialog(mode, errorMapId, baseVersion, draft) {
  const normalized = normalizeDraft(draft);
  return {
    mode,
    errorMapId,
    baseVersion,
    authoritativeDraft: { ...normalized, http_statuses: [...normalized.http_statuses] },
    draft: { ...normalized, http_statuses: [...normalized.http_statuses] },
    revision: 1,
    dirty: mode === "create",
    busy: false,
    conflicted: false,
    sideLatest: null,
    actionError: null,
    feedback: mode === "create" ? "填写规则后保存" : "",
  };
}

/** 创建不含 tenant owner 的安全默认草稿。 */
function emptyDraft() {
  return {
    provider_id: null,
    priority: 100,
    http_statuses: [],
    http_status_class: null,
    error_field: null,
    field_operator: null,
    field_pattern: null,
    message_operator: null,
    message_pattern: null,
    output_error_code: "gateway.upstream_request_invalid",
    status: "active",
  };
}

/** 将公开规则转换为可编辑白名单，hash、版本、source 和 owner 不进入草稿。 */
function draftFromRule(rule) {
  return normalizeDraft({
    provider_id: rule.provider_id,
    priority: rule.priority,
    http_statuses: rule.http_statuses,
    http_status_class: rule.http_status_class,
    error_field: rule.error_field,
    field_operator: rule.field_operator,
    field_pattern: rule.field_pattern,
    message_operator: rule.message_operator,
    message_pattern: rule.message_pattern,
    output_error_code: rule.output_error_code,
    status: rule.status,
  });
}

/** 规范化浏览器草稿；后端仍会独立执行所有范围、冲突和 guard 校验。 */
function normalizeDraft(value) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return {
    provider_id: nullableText(source.provider_id),
    priority: integerOr(source.priority, 100),
    http_statuses: [...new Set((Array.isArray(source.http_statuses) ? source.http_statuses : [])
      .filter((item) => Number.isSafeInteger(item)))]
      .sort((left, right) => left - right),
    http_status_class: nullableText(source.http_status_class),
    error_field: nullableText(source.error_field),
    field_operator: nullableText(source.field_operator),
    field_pattern: nullableText(source.field_pattern),
    message_operator: nullableText(source.message_operator),
    message_pattern: nullableText(source.message_pattern),
    output_error_code: text(source.output_error_code).trim(),
    status: text(source.status).trim() || "active",
  };
}

/** canonical 草稿固定 key 与数组顺序，确保网络未知重试不会因对象插入顺序更换 request_id。 */
function canonicalDraft(value) {
  const draft = normalizeDraft(value);
  return JSON.stringify([
    draft.provider_id,
    draft.priority,
    draft.http_statuses,
    draft.http_status_class,
    draft.error_field,
    draft.field_operator,
    draft.field_pattern,
    draft.message_operator,
    draft.message_pattern,
    draft.output_error_code,
    draft.status,
  ]);
}

/** 白名单复制规则 DTO，防止未来后端扩展字段被页面无意持有或渲染。 */
function copyRule(value) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return {
    error_map_id: text(source.error_map_id ?? source.provider_error_map_id),
    source: text(source.source),
    readonly: source.readonly === true || source.source === "builtin" || source.source === "guard",
    provider_id: nullableText(source.provider_id),
    provider_code: nullableText(source.provider_code),
    provider_name: nullableText(source.provider_name),
    priority: integerOr(source.priority, 0),
    ordinal: integerOr(source.ordinal, 0),
    http_statuses: Array.isArray(source.http_statuses)
      ? source.http_statuses.filter((item) => Number.isSafeInteger(item))
      : [],
    http_status_class: nullableText(source.http_status_class),
    error_field: nullableText(source.error_field),
    field_operator: nullableText(source.field_operator),
    field_pattern: nullableText(source.field_pattern),
    message_operator: nullableText(source.message_operator),
    message_pattern: nullableText(source.message_pattern),
    output_error_code: text(source.output_error_code),
    retryable: source.retryable === true,
    user_action: nullableText(source.user_action),
    status: text(source.status) || "active",
    version: validVersion(source.version) ?? 0,
    pattern_hash: nullableText(source.pattern_hash),
    updated_at: nullableText(source.updated_at),
  };
}

/** Provider 下拉只保存控制面明确公开的四个安全字段。 */
function copyProvider(value) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return {
    provider_id: text(source.provider_id),
    provider_code: text(source.provider_code),
    name: text(source.name),
    status: text(source.status),
  };
}

/** 复制配置 head；不会根据规则行自行推导或虚构 Gateway 已加载版本。 */
function copyHead(value, fallback = emptyHead()) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return {
    revision: nonnegativeInteger(source.head_revision ?? source.revision, fallback.revision),
    content_hash: text(source.content_hash ?? source.overlay_content_hash ?? fallback.content_hash),
    builtin_catalog_version: text(source.builtin_catalog_version ?? fallback.builtin_catalog_version),
    updated_at: nullableText(source.updated_at ?? fallback.updated_at),
  };
}

/** Head 初态只表示未知，不用 v0 伪造已同步状态。 */
function emptyHead() {
  return { revision: 0, content_hash: "", builtin_catalog_version: "", updated_at: null };
}

/** 复制 dialog 渲染字段；所有 controller、ticket、request_id 和 retry fingerprint 均留在闭包。 */
function copyDialog(value) {
  return {
    mode: value.mode,
    errorMapId: value.errorMapId,
    baseVersion: value.baseVersion,
    draft: { ...value.draft, http_statuses: [...value.draft.http_statuses] },
    dirty: value.dirty,
    busy: value.busy,
    conflicted: value.conflicted,
    sideLatest: value.sideLatest ? copyRule(value.sideLatest) : null,
    actionError: value.actionError ? { ...value.actionError } : null,
    feedback: value.feedback,
  };
}

/**
 * 白名单化历史规则引用。
 * tenant 以稳定资源 ID + 正版本定位，builtin 以稳定规则 ID + 不可变目录版本定位；guard 永不进入 handoff。
 */
function normalizeErrorMapReference(value) {
  const source = text(value?.source);
  const errorMapId = text(value?.errorMapId);
  // tenant 交接必须绑定稳定资源 ID 和请求时正版本，拒绝不完整或可注入的引用。
  if (source === "tenant") {
    const requestedVersion = validVersion(
      typeof value?.requestedVersion === "string" && /^\d+$/.test(value.requestedVersion)
        ? Number(value.requestedVersion)
        : value?.requestedVersion,
    );
    // 格式或版本不完整时立即丢弃交接，不能退化成宽泛列表查询后猜测资源。
    if (!TENANT_ERROR_MAP_ID_PATTERN.test(errorMapId) || !requestedVersion) {
      return null;
    }
    return Object.freeze({
      source,
      errorMapId,
      requestedVersion,
      requestedCatalogVersion: null,
    });
  }
  if (source === "builtin") {
    const requestedCatalogVersion = normalizeSafeIdentifier(value?.requestedCatalogVersion);
    // builtin 引用同时校验安全规则 ID 和不可变目录版本，guard 永远不会进入此分支。
    if (!SAFE_ERROR_MAP_ID_PATTERN.test(errorMapId) || !requestedCatalogVersion) {
      return null;
    }
    return Object.freeze({
      source,
      errorMapId,
      requestedVersion: null,
      requestedCatalogVersion,
    });
  }
  return null;
}

/** 复制已校验引用；返回对象不含 handoff owner、代次或任何日志请求标识。 */
function copyReference(value) {
  const normalized = normalizeErrorMapReference(value);
  return normalized ? { ...normalized } : null;
}

/** 安全标识禁止 trim 或 Unicode 规范化，防止两个外部 owner/目录值被折叠为同一身份。 */
function normalizeSafeIdentifier(value) {
  const candidate = text(value);
  return SAFE_ERROR_MAP_ID_PATTERN.test(candidate) ? candidate : "";
}

/** 统一错误快照只保留用户可见字段和 request_id，原始响应及未知 details 不进入 DOM。 */
function safeError(error, code, message) {
  return {
    status: error?.status,
    code: text(error?.code) || code,
    message: text(error?.message) || message,
    suggestion: text(error?.suggestion),
    requestId: text(error?.requestId ?? error?.request_id) || "-",
  };
}

/** 把可空 DTO 字段收敛为去空白字符串或 null。 */
function nullableText(value) {
  const normalized = text(value).trim();
  return normalized || null;
}

/** 严格版本只接受 JavaScript 可精确表达的正整数。 */
function validVersion(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

/** 非负整数用于分页和 revision；损坏值使用调用方给定的可信 fallback。 */
function nonnegativeInteger(value, fallback) {
  return Number.isSafeInteger(value) && value >= 0 ? value : fallback;
}

/** 正整数用于分页大小，非法服务端值不能参与按钮计算。 */
function positiveInteger(value, fallback) {
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

/** 通用整数规范化只服务展示草稿，最终范围由后端再次检查。 */
function integerOr(value, fallback) {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(parsed) ? parsed : fallback;
}

/** 把未知值安全转换为字符串，HTML 层仍会二次转义。 */
function text(value) {
  return value == null ? "" : String(value);
}
