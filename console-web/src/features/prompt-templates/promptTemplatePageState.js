import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";
import { createPromptTemplateRequestId } from "../../lib/promptTemplates.js";

/** PAGE-094 仅接受后端公开资源 ID；tenant owner 不从 ID 推导，仍由 Session 和复合关系校验。 */
const TEMPLATE_ID_PATTERN = /^ptpl_[A-Za-z0-9_-]{1,120}$/;
const VERSION_ID_PATTERN = /^ptv_[A-Za-z0-9_-]{1,120}$/;
const VARIABLE_NAME_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const RESERVED_VARIABLE_NAMES = new Set(["constructor", "prototype", "__proto__"]);
const STATUS_VALUES = new Set(["active", "disabled"]);
const FILTER_STATUS_VALUES = new Set(["", "active", "disabled"]);

/** 从实时认证快照构造 PAGE-094 读取 owner；角色名永远不能替代精确 permission。 */
export function promptTemplateReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PROMPT_TEMPLATE_READ);
}

/** 从实时认证快照构造 PAGE-094 写 owner；所有 mutation 必须与 read owner 分离冻结。 */
export function promptTemplateWriteIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PROMPT_TEMPLATE_WRITE);
}

/**
 * 创建 PAGE-094 私有状态机。
 *
 * list、detail、definition、mutation、conflict 五条 lane 具有独立 controller 和 generation；正文、
 * 变量草稿、稳定写 request_id 与 canonical fingerprint 均只存在于闭包，snapshot 只供当前 DOM 同步使用。
 */
export function createPromptTemplatePageState({
  requestIdFactory = createPromptTemplateRequestId,
} = {}) {
  let readIdentity = "";
  let writeIdentity = "";
  let navigation = "";
  let refreshGeneration = 0;
  let pageGeneration = 0;
  let filters = { status: "", search: "", limit: 50, offset: 0 };
  let options = emptyOptions();
  let templates = [];
  let total = 0;
  let selectedTemplateId = "";
  let detail = null;
  let definition = null;
  let detailTab = "current";
  let dialog = null;
  let pageError = null;
  let retryMutation = null;
  const lanes = {
    list: createLane(),
    detail: createLane(),
    definition: createLane(),
    mutation: createLane(),
    conflict: createLane(),
  };

  /** 同步完整读写授权 owner；任一身份、导航或刷新代次漂移都先销毁旧正文和全部 ticket。 */
  function syncOwner(owner) {
    const nextReadIdentity = String(owner?.readIdentity ?? "");
    const nextWriteIdentity = String(owner?.writeIdentity ?? "");
    const nextNavigation = owner?.navigation === "prompt-templates" ? "prompt-templates" : "";
    const nextRefreshGeneration = Number(owner?.refreshGeneration);
    if (!nextReadIdentity || !nextNavigation || !Number.isSafeInteger(nextRefreshGeneration)) {
      leave();
      return false;
    }
    if (
      readIdentity !== nextReadIdentity
      || writeIdentity !== nextWriteIdentity
      || navigation !== nextNavigation
      || refreshGeneration !== nextRefreshGeneration
    ) {
      leave();
      readIdentity = nextReadIdentity;
      writeIdentity = nextWriteIdentity;
      navigation = nextNavigation;
      refreshGeneration = nextRefreshGeneration;
      pageGeneration += 1;
    }
    return true;
  }

  /** 更新本地查询条件；查询字段有界且不包含正文，实际过滤仍由同租户后端执行。 */
  function setFilters(next) {
    if (lanes.list.ticket || lanes.mutation.ticket) return false;
    const status = String(next?.status ?? filters.status).trim();
    const search = String(next?.search ?? filters.search).trim().slice(0, 120);
    const limit = boundedInteger(next?.limit, 1, 100, filters.limit);
    const offset = nonnegativeInteger(next?.offset, filters.offset);
    if (!FILTER_STATUS_VALUES.has(status)) return false;
    filters = { status, search, limit, offset };
    return true;
  }

  /** 开始列表与表单 options 的同轮读取；只取消上一条 list lane，不影响其他四条 lane。 */
  function beginList() {
    if (!ownedForRead()) return null;
    const ticket = beginLane("list", {
      filters: Object.freeze({ ...filters }),
      requestId: requestIdFactory("list"),
      optionsRequestId: requestIdFactory("options"),
    });
    pageError = null;
    return ticket;
  }

  /** 接纳列表和 options；只允许安全投影进入长期页面状态，正文绝不会来自该 lane。 */
  function acceptList(ticket, payload, optionsPayload) {
    if (!isCurrentLane("list", ticket)) return false;
    const source = Array.isArray(payload?.prompt_templates)
      ? payload.prompt_templates
      : (Array.isArray(payload?.items) ? payload.items : []);
    const nextTemplates = source.map(copyTemplate).filter((item) => item.prompt_template_id);
    if (
      nextTemplates.length !== source.length
      || nextTemplates.some((item) => !validTemplateProjection(item))
      || new Set(nextTemplates.map((item) => item.prompt_template_id)).size !== nextTemplates.length
    ) {
      rejectLane("list", ticket, integrityError());
      return false;
    }
    templates = nextTemplates;
    total = nonnegativeInteger(payload?.total, nextTemplates.length);
    options = copyOptions(optionsPayload);
    const selectionStillExists = templates.some((item) => item.prompt_template_id === selectedTemplateId);
    if (!selectionStillExists) {
      selectedTemplateId = templates[0]?.prompt_template_id ?? "";
      detail = null;
      clearDefinition();
    }
    finishLane("list", ticket);
    pageError = null;
    return true;
  }

  /** 选择列表模板；切换资源时先取消旧 detail/definition 并同步清空解密正文。 */
  function selectTemplate(promptTemplateId) {
    const templateId = String(promptTemplateId ?? "");
    if (
      lanes.mutation.ticket
      || dialog
      || !templates.some((item) => item.prompt_template_id === templateId)
    ) return false;
    if (templateId === selectedTemplateId) return true;
    abortLane("detail");
    abortLane("definition");
    selectedTemplateId = templateId;
    detail = null;
    clearDefinition();
    detailTab = "current";
    pageError = null;
    return true;
  }

  /** 开始当前选择的精确详情 GET；同 lane 新请求只淘汰旧详情，不影响正文或 mutation。 */
  function beginDetail(promptTemplateId = selectedTemplateId) {
    const templateId = String(promptTemplateId ?? "");
    if (!ownedForRead() || !TEMPLATE_ID_PATTERN.test(templateId)) return null;
    return beginLane("detail", {
      promptTemplateId: templateId,
      requestId: requestIdFactory("detail"),
    });
  }

  /** 接纳同一模板详情和版本历史；metadata_revision 与 version_number 使用不同字段保存。 */
  function acceptDetail(ticket, payload) {
    if (!isCurrentLane("detail", ticket) || ticket.promptTemplateId !== selectedTemplateId) return false;
    const nextDetail = copyDetail(payload);
    if (
      nextDetail.prompt_template_id !== ticket.promptTemplateId
      || !validDetailProjection(nextDetail)
    ) {
      rejectLane("detail", ticket, integrityError());
      return false;
    }
    detail = nextDetail;
    upsertTemplate(nextDetail);
    finishLane("detail", ticket);
    pageError = null;
    return true;
  }

  /** 开始精确版本定义读取；历史版本只读，调用版本仍只能由后端 current head 决定。 */
  function beginDefinition(promptTemplateVersionId) {
    const versionId = String(promptTemplateVersionId ?? "");
    if (
      !ownedForRead()
      || !detail
      || !VERSION_ID_PATTERN.test(versionId)
      || !detail.versions.some((item) => item.prompt_template_version_id === versionId)
    ) return null;
    clearDefinition();
    return beginLane("definition", {
      promptTemplateId: detail.prompt_template_id,
      promptTemplateVersionId: versionId,
      requestId: requestIdFactory("definition"),
    });
  }

  /** 接纳解密定义；正文仅保存在闭包并交给 hydration，不复制未知响应字段。 */
  function acceptDefinition(ticket, payload) {
    if (!isCurrentLane("definition", ticket)) return false;
    const nextDefinition = copyDefinition(payload);
    if (
      nextDefinition.prompt_template_id !== ticket.promptTemplateId
      || nextDefinition.prompt_template_version_id !== ticket.promptTemplateVersionId
      || !validDefinitionProjection(nextDefinition)
    ) {
      rejectLane("definition", ticket, integrityError());
      return false;
    }
    definition = nextDefinition;
    finishLane("definition", ticket);
    pageError = null;
    return true;
  }

  /** 切换详情标签；当前版本和历史只影响展示，不改变服务端 head 或选择资源。 */
  function setDetailTab(value) {
    if (!detail || !["current", "history"].includes(value)) return false;
    detailTab = value;
    return true;
  }

  /** 打开创建表单；无独立 write owner 时不生成受保护写 DOM。 */
  function openCreate() {
    if (!ownedForWrite() || dialog || lanes.mutation.ticket) return false;
    dialog = createDialog("create", null, 0, emptyDefinitionDraft());
    retryMutation = null;
    return true;
  }

  /** 打开元数据 CAS 表单；authoritative base 与本地 draft 分开保存供 409 恢复。 */
  function openMetadata() {
    if (!ownedForWrite() || !detail || dialog || lanes.mutation.ticket) return false;
    const draft = {
      name: detail.name,
      description: detail.description,
      status: detail.status,
    };
    dialog = createDialog("metadata", detail.prompt_template_id, detail.metadata_revision, draft);
    retryMutation = null;
    return true;
  }

  /** 打开发布新版本表单；已读取 current definition 时可在内存预填，但 renderer 仍不得字符串插值正文。 */
  function openPublish() {
    if (!ownedForWrite() || !detail || dialog || lanes.mutation.ticket) return false;
    const current = definition?.prompt_template_version_id === detail.current_version_id
      ? definition
      : null;
    const draft = current ? definitionDraftFrom(current) : emptyDefinitionDraft();
    dialog = createDialog("publish", detail.prompt_template_id, detail.metadata_revision, draft);
    // 发布始终是新不可变事实，即使定义与 current 一样也必须有明确 change_summary，因此初态视为未完成而非 no-op。
    dialog.dirty = true;
    retryMutation = null;
    return true;
  }

  /** 更新名称、描述、状态或完整定义草稿；正文变化只留在闭包并推进 revision。 */
  function updateDialog(patch) {
    if (!dialog || dialog.busy || dialog.conflicted || !patch || typeof patch !== "object") return false;
    const next = normalizeDialogDraft(dialog.mode, { ...dialog.draft, ...patch });
    dialog.draft = next;
    dialog.revision += 1;
    dialog.dirty = dialog.mode === "publish"
      ? true
      : canonicalDraft(dialog.mode, next) !== canonicalDraft(dialog.mode, dialog.authoritativeDraft);
    dialog.actionError = null;
    dialog.feedback = dialog.dirty ? "存在未保存修改" : "";
    retryMutation = null;
    return true;
  }

  /** 新增变量行；变量名、label 和 value 均不写入 HTML 字符串，由 hydration 创建输入控件。 */
  function addVariable() {
    if (!dialog || !["create", "publish"].includes(dialog.mode) || dialog.busy || dialog.conflicted) return false;
    if (dialog.draft.variables.length >= 32) return false;
    return updateDialog({
      variables: [...dialog.draft.variables, { name: "", label: "", required: true, max_chars: 1000 }],
    });
  }

  /** 更新单个变量定义；索引只定位当前闭包数组，后端仍会校验占位符 exact-set。 */
  function updateVariable(index, patch) {
    if (!Number.isSafeInteger(index) || index < 0 || index >= (dialog?.draft?.variables?.length ?? 0)) return false;
    const variables = dialog.draft.variables.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...patch } : item
    ));
    return updateDialog({ variables });
  }

  /** 删除变量行；正文中的占位符关系由后端在发布前权威复核，浏览器不尝试执行模板语法。 */
  function removeVariable(index) {
    if (!Number.isSafeInteger(index) || index < 0 || index >= (dialog?.draft?.variables?.length ?? 0)) return false;
    return updateDialog({
      variables: dialog.draft.variables.filter((_item, itemIndex) => itemIndex !== index),
    });
  }

  /** 调整项目或推荐模型绑定；数组顺序保持发布意图，tenant owner 始终由后端恢复。 */
  function setBinding(kind, resourceId, enabled) {
    if (!dialog || !["create", "publish"].includes(dialog.mode)) return false;
    if (kind === "project" && dialog.draft.project_scope !== "selected") return false;
    const field = kind === "project" ? "project_ids" : "recommended_model_alias_ids";
    const allowed = kind === "project"
      ? options.projects.some((item) => item.project_id === resourceId)
      : options.model_aliases.some((item) => item.model_alias_id === resourceId);
    if (!allowed) return false;
    const current = dialog.draft[field];
    const next = enabled
      ? [...current.filter((item) => item !== resourceId), resourceId]
      : current.filter((item) => item !== resourceId);
    return updateDialog({ [field]: next });
  }

  /** 调整推荐模型展示顺序；该顺序只进入版本定义，不会修改任何 ChatSession 或路由策略。 */
  function moveRecommendedModel(resourceId, direction) {
    if (!dialog || !["create", "publish"].includes(dialog.mode) || dialog.busy || dialog.conflicted) return false;
    const current = [...dialog.draft.recommended_model_alias_ids];
    const index = current.indexOf(resourceId);
    const nextIndex = direction === "up" ? index - 1 : index + 1;
    if (index < 0 || nextIndex < 0 || nextIndex >= current.length) return false;
    [current[index], current[nextIndex]] = [current[nextIndex], current[index]];
    return updateDialog({ recommended_model_alias_ids: current });
  }

  /** 为当前写意图生成唯一 mutation ticket；网络未知且 canonical intent 未变时复用原 request_id。 */
  function beginMutation() {
    if (!ownedForWrite() || !dialog || dialog.busy || dialog.conflicted || !dialog.dirty) return null;
    const payload = mutationPayload(dialog);
    if (!payload) {
      dialog.actionError = publicError(null, "prompt_template.request_invalid", "模板表单不完整");
      return null;
    }
    const fingerprint = canonicalMutation(dialog.mode, dialog.templateId, payload);
    const requestId = retryMutation?.fingerprint === fingerprint
      ? retryMutation.requestId
      : requestIdFactory(dialog.mode === "metadata" ? "patch" : dialog.mode);
    const ticket = beginLane("mutation", {
      kind: dialog.mode,
      promptTemplateId: dialog.templateId,
      dialogRevision: dialog.revision,
      baseMetadataRevision: dialog.baseMetadataRevision,
      payload: deepFreezePayload(payload),
      fingerprint,
      requestId,
    }, { abortPrevious: false });
    if (!ticket) return null;
    dialog.busy = true;
    dialog.actionError = null;
    dialog.feedback = "保存中";
    return ticket;
  }

  /** 接纳创建、PATCH 或发布成功；资源、CAS 与 version identity 任一不一致都失败关闭。 */
  function acceptMutation(ticket, payload) {
    if (!isCurrentLane("mutation", ticket) || dialog?.revision !== ticket.dialogRevision) return false;
    const template = copyTemplate(payload?.template ?? payload?.prompt_template);
    const version = copyVersion(payload?.version ?? payload?.template_version);
    const expectedTemplateId = ticket.promptTemplateId || template.prompt_template_id;
    const validTemplate = validTemplateProjection(template)
      && template.prompt_template_id === expectedTemplateId
      && template.metadata_revision >= (ticket.baseMetadataRevision || 0);
    const needsVersion = ticket.kind === "create" || ticket.kind === "publish";
    const validVersion = !needsVersion || (
      VERSION_ID_PATTERN.test(version.prompt_template_version_id)
      && version.prompt_template_id === template.prompt_template_id
      && positiveInteger(version.version_number, 0)
      && version.state === "published"
      && ["all", "selected"].includes(version.project_scope)
    );
    if (!validTemplate || !validVersion) {
      rejectLane("mutation", ticket, integrityError(), { preserveDialog: true });
      return false;
    }
    finishLane("mutation", ticket);
    retryMutation = null;
    upsertTemplate(template);
    selectedTemplateId = template.prompt_template_id;
    detail = mergeMutationDetail(detail, template, version, needsVersion);
    if (needsVersion) {
      definition = {
        prompt_template_id: template.prompt_template_id,
        prompt_template_version_id: version.prompt_template_version_id,
        version_number: version.version_number,
        ...definitionDraftFrom(dialog.draft),
      };
    }
    dialog = null;
    pageError = null;
    return true;
  }

  /**
   * 处理写失败。
   *
   * 409 保留 authoritative base 与本地草稿；5xx/网络未知保留同指纹 request_id；其他 HTTP 终态换号重试。
   */
  function rejectMutation(ticket, candidate) {
    if (!isCurrentLane("mutation", ticket) || !dialog) return false;
    finishLane("mutation", ticket);
    dialog.busy = false;
    dialog.actionError = publicError(candidate, "prompt_template.unavailable", "模板保存失败");
    if (Number(candidate?.status) === 409) {
      dialog.conflicted = true;
      dialog.feedback = "服务器版本已变化，原草稿仍保留";
      retryMutation = null;
    } else if (!candidate?.status || Number(candidate.status) >= 500) {
      dialog.feedback = "网络结果未知，再次保存将复用同一 request_id";
      retryMutation = { fingerprint: ticket.fingerprint, requestId: ticket.requestId };
    } else {
      dialog.feedback = "保存失败，请修正后重试";
      retryMutation = null;
    }
    return true;
  }

  /** 409 后启动独立精确 GET；该 lane 与失败写和下一次写均使用不同 request_id。 */
  function beginConflictDetail() {
    if (!ownedForRead() || !dialog?.conflicted || !TEMPLATE_ID_PATTERN.test(dialog.templateId)) return null;
    return beginLane("conflict", {
      promptTemplateId: dialog.templateId,
      requestId: requestIdFactory("conflict"),
    });
  }

  /** 接纳冲突最新值但不覆盖用户草稿；只有用户显式采纳才更新 base metadata_revision。 */
  function acceptConflictDetail(ticket, payload) {
    if (!isCurrentLane("conflict", ticket) || !dialog?.conflicted) return false;
    const latest = copyDetail(payload);
    if (latest.prompt_template_id !== ticket.promptTemplateId) {
      rejectLane("conflict", ticket, integrityError(), { preserveDialog: true });
      return false;
    }
    dialog.sideLatest = latest;
    dialog.feedback = `已读取服务器 revision ${latest.metadata_revision}，原草稿仍保留`;
    finishLane("conflict", ticket);
    return true;
  }

  /** 冲突 GET 失败只更新当前 dialog；不能结束认证或丢弃原草稿。 */
  function rejectConflictDetail(ticket, candidate) {
    if (!isCurrentLane("conflict", ticket) || !dialog?.conflicted) return false;
    finishLane("conflict", ticket);
    dialog.actionError = publicError(candidate, "prompt_template.unavailable", "最新模板读取失败");
    dialog.feedback = "原草稿仍保留";
    return true;
  }

  /** 用户显式采纳最新 authoritative base；本地草稿保留，并在新 revision 上重新提交。 */
  function adoptConflict() {
    if (!dialog?.conflicted || dialog.busy || !dialog.sideLatest) return false;
    const latest = dialog.sideLatest;
    dialog.baseMetadataRevision = latest.metadata_revision;
    if (dialog.mode === "metadata") {
      dialog.authoritativeDraft = normalizeDialogDraft("metadata", {
        name: latest.name,
        description: latest.description,
        status: latest.status,
      });
      dialog.dirty = canonicalDraft("metadata", dialog.draft)
        !== canonicalDraft("metadata", dialog.authoritativeDraft);
    } else {
      dialog.dirty = true;
    }
    dialog.conflicted = false;
    dialog.sideLatest = null;
    dialog.actionError = null;
    dialog.feedback = `已采纳服务器 revision ${latest.metadata_revision}，本地草稿仍保留`;
    retryMutation = null;
    return true;
  }

  /** 非 busy 表单可关闭；关闭同步清空正文草稿、冲突响应和未知结果 retry ticket。 */
  function closeDialog() {
    if (!dialog || dialog.busy) return false;
    abortLane("conflict");
    dialog = null;
    retryMutation = null;
    return true;
  }

  /** 统一拒绝非 mutation lane；公开错误永不复制原始异常、正文或未知详情。 */
  function reject(ticket, candidate) {
    const laneName = ticket?.lane;
    if (!laneName || laneName === "mutation" || laneName === "conflict") return false;
    return rejectLane(laneName, ticket, candidate);
  }

  /** active 表单、正文、冲突或在途请求存在时阻止全壳 DOM 替换。 */
  function blocksShellDomReplacement() {
    return Boolean(
      definition
      || dialog
      || Object.values(lanes).some((lane) => lane.ticket),
    );
  }

  /** 返回当前渲染快照；controller、owner、Token、request_id 和 mutation fingerprint 均不投影。 */
  function snapshot() {
    return {
      state: pageError && !templates.length ? "error" : "ok",
      filters: { ...filters },
      options: copyOptions(options),
      templates: templates.map(copyTemplate),
      total,
      selectedTemplateId,
      detail: detail ? copyDetail(detail) : null,
      definition: definition ? copyDefinition(definition) : null,
      detailTab,
      dialog: dialog ? copyDialog(dialog) : null,
      pageError: pageError ? { ...pageError } : null,
      busy: Object.values(lanes).some((lane) => lane.ticket),
      canWrite: Boolean(writeIdentity),
    };
  }

  /** 离页、注销、401/403、pagehide 或 owner 漂移时同步 abort 并销毁全部敏感内存。 */
  function leave() {
    for (const laneName of Object.keys(lanes)) abortLane(laneName);
    readIdentity = "";
    writeIdentity = "";
    navigation = "";
    refreshGeneration = 0;
    pageGeneration += 1;
    options = emptyOptions();
    templates = [];
    total = 0;
    selectedTemplateId = "";
    detail = null;
    clearDefinition();
    dialog = null;
    pageError = null;
    retryMutation = null;
    detailTab = "current";
  }

  /** 判断当前状态是否具有完整 read owner。 */
  function ownedForRead() {
    return Boolean(readIdentity && navigation === "prompt-templates");
  }

  /** 判断当前状态是否同时具有独立 write owner。 */
  function ownedForWrite() {
    return Boolean(ownedForRead() && writeIdentity);
  }

  /** 创建指定 lane ticket；默认只 abort 同 lane，确保跨 lane 请求互不取消。 */
  function beginLane(laneName, extra, { abortPrevious = true } = {}) {
    const lane = lanes[laneName];
    if (!lane || (!abortPrevious && lane.ticket)) return null;
    if (abortPrevious) abortLane(laneName);
    lane.generation += 1;
    lane.controller = new AbortController();
    lane.ticket = Object.freeze({
      lane: laneName,
      generation: lane.generation,
      pageGeneration,
      readIdentity,
      writeIdentity: laneName === "mutation" ? writeIdentity : "",
      navigation,
      refreshGeneration,
      controller: lane.controller,
      signal: lane.controller.signal,
      ...extra,
    });
    return lane.ticket;
  }

  /** 比较 lane、页面、授权身份和刷新代次，淘汰任何迟到响应。 */
  function isCurrentLane(laneName, ticket) {
    const lane = lanes[laneName];
    return Boolean(
      lane?.ticket === ticket
      && ticket?.lane === laneName
      && !ticket.signal.aborted
      && ticket.generation === lane.generation
      && ticket.pageGeneration === pageGeneration
      && ticket.readIdentity === readIdentity
      && ticket.navigation === navigation
      && ticket.refreshGeneration === refreshGeneration
      && (laneName !== "mutation" || ticket.writeIdentity === writeIdentity),
    );
  }

  /** 完成当前 lane；迟到 finally 不能清除较新的 ticket。 */
  function finishLane(laneName, ticket) {
    const lane = lanes[laneName];
    if (lane?.ticket === ticket) {
      lane.ticket = null;
      lane.controller = null;
    }
  }

  /** 取消单条 lane 并推进 generation；其他 lane 保持运行。 */
  function abortLane(laneName) {
    const lane = lanes[laneName];
    if (!lane) return;
    lane.controller?.abort();
    lane.controller = null;
    lane.ticket = null;
    lane.generation += 1;
  }

  /** 清空解密正文，避免切换模板或版本后旧内容被下一次 hydration 恢复。 */
  function clearDefinition() {
    definition = null;
  }

  /** 将公开错误绑定到当前 lane；401/403 留给 main 处理认证刷新，但本状态先清敏感内存。 */
  function rejectLane(laneName, ticket, candidate, { preserveDialog = false } = {}) {
    if (!isCurrentLane(laneName, ticket)) return false;
    const nextError = publicError(candidate);
    finishLane(laneName, ticket);
    if (Number(candidate?.status) === 401 || Number(candidate?.status) === 403) {
      leave();
      return true;
    }
    if (preserveDialog && dialog) {
      dialog.busy = false;
      dialog.actionError = nextError;
    } else {
      pageError = nextError;
    }
    return true;
  }

  /** 将模板安全投影插入或替换列表，保持服务端更新时间顺序之外不自行猜测排序。 */
  function upsertTemplate(value) {
    const next = copyTemplate(value);
    const index = templates.findIndex((item) => item.prompt_template_id === next.prompt_template_id);
    templates = index >= 0
      ? templates.map((item, itemIndex) => (itemIndex === index ? next : item))
      : [next, ...templates];
  }

  return Object.freeze({
    syncOwner,
    setFilters,
    beginList,
    acceptList,
    selectTemplate,
    beginDetail,
    acceptDetail,
    beginDefinition,
    acceptDefinition,
    setDetailTab,
    openCreate,
    openMetadata,
    openPublish,
    updateDialog,
    addVariable,
    updateVariable,
    removeVariable,
    setBinding,
    moveRecommendedModel,
    beginMutation,
    acceptMutation,
    rejectMutation,
    beginConflictDetail,
    acceptConflictDetail,
    rejectConflictDetail,
    adoptConflict,
    closeDialog,
    reject,
    blocksShellDomReplacement,
    snapshot,
    leave,
  });
}

/** 创建独立异步 lane 容器；controller 永不进入公开 snapshot。 */
function createLane() {
  return { generation: 0, controller: null, ticket: null };
}

/** 创建 dialog，authoritativeDraft 与 draft 必须深复制，防止数组共享破坏 dirty 比较。 */
function createDialog(mode, templateId, baseMetadataRevision, draft) {
  const normalized = normalizeDialogDraft(mode, draft);
  return {
    mode,
    templateId,
    baseMetadataRevision,
    authoritativeDraft: cloneDraft(normalized),
    draft: cloneDraft(normalized),
    revision: 1,
    dirty: mode === "create",
    busy: false,
    conflicted: false,
    sideLatest: null,
    actionError: null,
    feedback: mode === "create" ? "填写模板后创建" : "",
  };
}

/** 创建不含 tenant、actor 或服务端 ID 的空定义草稿。 */
function emptyDefinitionDraft() {
  return {
    name: "",
    description: "",
    content: "",
    variables: [],
    project_scope: "all",
    project_ids: [],
    recommended_model_alias_ids: [],
    change_summary: "",
  };
}

/** 把已授权解密定义复制为发布草稿；不会复制密文、HMAC、owner 或 request_id。 */
function definitionDraftFrom(value) {
  const source = value && typeof value === "object" ? value : {};
  return {
    content: String(source.content ?? ""),
    variables: Array.isArray(source.variables) ? source.variables.map(copyVariable) : [],
    project_scope: source.project_scope === "selected" ? "selected" : "all",
    project_ids: uniqueTextArray(source.project_ids ?? source.applicable_project_ids, 128, 100),
    recommended_model_alias_ids: uniqueTextArray(source.recommended_model_alias_ids, 128, 8),
    change_summary: String(source.change_summary ?? ""),
  };
}

/** 按 dialog 模式规范化草稿；只做交互边界，服务端仍执行字节、占位符和 owner 权威校验。 */
function normalizeDialogDraft(mode, value) {
  const source = value && typeof value === "object" ? value : {};
  if (mode === "metadata") {
    return {
      name: safeText(source.name, 120),
      description: safeText(source.description, 1000),
      status: STATUS_VALUES.has(source.status) ? source.status : "active",
    };
  }
  const projectScope = source.project_scope === "selected" ? "selected" : "all";
  const definition = {
    content: String(source.content ?? "").slice(0, 32768),
    variables: Array.isArray(source.variables)
      ? source.variables.slice(0, 32).map(copyVariable)
      : [],
    project_scope: projectScope,
    // all scope 不携带冗余项目 ID，避免用户切换范围后生成后端必然拒绝的歧义请求。
    project_ids: projectScope === "selected" ? uniqueTextArray(source.project_ids, 128, 100) : [],
    recommended_model_alias_ids: uniqueTextArray(source.recommended_model_alias_ids, 128, 8),
    change_summary: safeText(source.change_summary, 300),
  };
  return mode === "create"
    ? {
        name: safeText(source.name, 120),
        description: safeText(source.description, 1000),
        ...definition,
      }
    : definition;
}

/** 深复制草稿中的变量和绑定数组，正文仍只驻留当前 JS 内存。 */
function cloneDraft(value) {
  return {
    ...value,
    ...(Array.isArray(value?.variables) ? { variables: value.variables.map(copyVariable) } : {}),
    ...(Array.isArray(value?.project_ids) ? { project_ids: [...value.project_ids] } : {}),
    ...(Array.isArray(value?.recommended_model_alias_ids)
      ? { recommended_model_alias_ids: [...value.recommended_model_alias_ids] }
      : {}),
  };
}

/** 将 dialog 转为后端 exact-set 请求；校验失败返回 null，绝不发送部分定义。 */
function mutationPayload(value) {
  const draft = value.draft;
  if (value.mode === "metadata") {
    if (!validMetadataRevision(value.baseMetadataRevision) || !draft.name.trim()) return null;
    return {
      expected_revision: value.baseMetadataRevision,
      name: draft.name,
      description: draft.description,
      status: draft.status,
    };
  }
  if (
    !draft.content.trim()
    || !draft.change_summary.trim()
    || (draft.project_scope === "selected" && !draft.project_ids.length)
    || !validVariables(draft.variables)
  ) return null;
  const definition = {
    content: draft.content,
    variables: draft.variables.map(copyVariable),
    project_scope: draft.project_scope,
    project_ids: [...draft.project_ids],
    recommended_model_alias_ids: [...draft.recommended_model_alias_ids],
    change_summary: draft.change_summary,
  };
  if (value.mode === "create") {
    if (!draft.name.trim()) return null;
    return { name: draft.name, description: draft.description, ...definition };
  }
  return validMetadataRevision(value.baseMetadataRevision)
    ? { expected_revision: value.baseMetadataRevision, ...definition }
    : null;
}

/** 验证变量定义的最小前端边界；服务端仍会验证占位符 exact-set 与 UTF-8 字节数。 */
function validVariables(values) {
  if (!Array.isArray(values) || values.length > 32) return false;
  const names = values.map((item) => item.name);
  return values.every((item) => (
    VARIABLE_NAME_PATTERN.test(item.name)
    && !RESERVED_VARIABLE_NAMES.has(item.name)
    && item.label.trim().length > 0
    && typeof item.required === "boolean"
    && Number.isSafeInteger(item.max_chars)
    && item.max_chars >= 1
    && item.max_chars <= 8000
  )) && new Set(names).size === names.length;
}

/** canonical 草稿用于 dirty 比较；固定键顺序，不依赖对象插入顺序。 */
function canonicalDraft(mode, draft) {
  const value = normalizeDialogDraft(mode, draft);
  return mode === "metadata"
    ? JSON.stringify([value.name, value.description, value.status])
    : JSON.stringify([
        mode === "create" ? value.name : null,
        mode === "create" ? value.description : null,
        value.content,
        value.variables.map((item) => [item.name, item.label, item.required, item.max_chars]),
        value.project_scope,
        value.project_ids,
        value.recommended_model_alias_ids,
        value.change_summary,
      ]);
}

/** canonical mutation 绑定操作、资源和完整请求体，确保变更任何字段都会换 request_id。 */
function canonicalMutation(kind, templateId, payload) {
  return JSON.stringify([kind, templateId, payload]);
}

/** 冻结 mutation payload 的数组和变量对象，避免点击后 DOM 输入继续修改在途意图。 */
function deepFreezePayload(payload) {
  const clone = {
    ...payload,
    ...(Array.isArray(payload.variables)
      ? { variables: Object.freeze(payload.variables.map((item) => Object.freeze({ ...item }))) }
      : {}),
    ...(Array.isArray(payload.project_ids) ? { project_ids: Object.freeze([...payload.project_ids]) } : {}),
    ...(Array.isArray(payload.recommended_model_alias_ids)
      ? { recommended_model_alias_ids: Object.freeze([...payload.recommended_model_alias_ids]) }
      : {}),
  };
  return Object.freeze(clone);
}

/** 白名单复制模板投影，并统一命名 metadata_revision，避免与 version_number 混用。 */
function copyTemplate(value) {
  const source = value && typeof value === "object" ? value : {};
  const current = source.current_version && typeof source.current_version === "object"
    ? source.current_version
    : {};
  return {
    prompt_template_id: safeText(source.prompt_template_id, 128),
    name: safeText(source.name, 120),
    description: safeText(source.description, 1000),
    status: STATUS_VALUES.has(source.status) ? source.status : "",
    metadata_revision: validMetadataRevision(source.metadata_revision ?? source.revision) ?? 0,
    current_version_id: safeText(source.current_version_id ?? current.prompt_template_version_id, 128),
    current_version_number: positiveInteger(source.current_version_number ?? current.version_number, 0),
    project_count: nonnegativeInteger(source.project_count ?? current.project_count, 0),
    recommended_model_count: nonnegativeInteger(
      source.recommended_model_count ?? current.recommended_model_count,
      0,
    ),
    updated_at: safeText(source.updated_at, 64),
    created_at: safeText(source.created_at, 64),
  };
}

/** 白名单复制详情和版本历史；未知扩展、密文、tenant_id 和 owner 字段全部丢弃。 */
function copyDetail(payload) {
  const source = payload?.template ?? payload?.prompt_template ?? payload ?? {};
  const template = copyTemplate(source);
  const versionSource = Array.isArray(payload?.versions)
    ? payload.versions
    : (Array.isArray(source.versions) ? source.versions : []);
  const versions = versionSource.map(copyVersion).filter((item) => item.prompt_template_version_id);
  return {
    ...template,
    versions,
  };
}

/** 白名单复制不可变版本元数据；version_number 与 metadata_revision 永远分开存放。 */
function copyVersion(value) {
  const source = value && typeof value === "object" ? value : {};
  return {
    prompt_template_version_id: safeText(source.prompt_template_version_id, 128),
    prompt_template_id: safeText(source.prompt_template_id, 128),
    version_number: positiveInteger(source.version_number, 0),
    state: source.state === "published" ? "published" : "",
    project_scope: ["all", "selected"].includes(source.project_scope) ? source.project_scope : "",
    project_ids: uniqueTextArray(source.project_ids ?? source.applicable_project_ids, 128, 100),
    recommended_model_alias_ids: uniqueTextArray(source.recommended_model_alias_ids, 128, 8),
    variable_count: nonnegativeInteger(source.variable_count, 0),
    project_count: nonnegativeInteger(source.project_count, 0),
    recommended_model_count: nonnegativeInteger(source.recommended_model_count, 0),
    created_at: safeText(source.created_at, 64),
    published_at: safeText(source.published_at, 64),
  };
}

/** 白名单复制解密定义；content/change_summary 只供当前 hydration 和表单预填。 */
function copyDefinition(payload) {
  const version = payload?.version ?? payload?.template_version ?? {};
  const source = payload?.definition ?? version?.definition ?? payload ?? {};
  return {
    prompt_template_id: safeText(
      payload?.prompt_template_id ?? version?.prompt_template_id ?? source?.prompt_template_id,
      128,
    ),
    prompt_template_version_id: safeText(
      payload?.prompt_template_version_id ?? version?.prompt_template_version_id ?? source?.prompt_template_version_id,
      128,
    ),
    version_number: positiveInteger(
      payload?.version_number ?? version?.version_number ?? source?.version_number,
      0,
    ),
    content: String(source?.content ?? "").slice(0, 32768),
    variables: Array.isArray(source?.variables) ? source.variables.slice(0, 32).map(copyVariable) : [],
    project_scope: ["all", "selected"].includes(source?.project_scope) ? source.project_scope : "",
    project_ids: uniqueTextArray(source?.project_ids ?? source?.applicable_project_ids, 128, 100),
    recommended_model_alias_ids: uniqueTextArray(source?.recommended_model_alias_ids, 128, 8),
    change_summary: safeText(source?.change_summary, 300),
  };
}

/** 白名单复制变量 schema；实际调用值不属于 PAGE-094 DTO。 */
function copyVariable(value) {
  const source = value && typeof value === "object" ? value : {};
  return {
    name: safeText(source.name, 64),
    label: safeText(source.label, 80),
    required: source.required === true,
    max_chars: boundedInteger(source.max_chars, 1, 8000, 1000),
  };
}

/** 复制模板表单最小候选目录；只接受 active Project 和 ModelAlias 安全字段。 */
function copyOptions(payload) {
  return {
    projects: Array.isArray(payload?.projects)
      ? payload.projects.map((item) => ({
          project_id: safeText(item?.project_id, 128),
          name: safeText(item?.name, 160),
          status: item?.status === "active" ? "active" : "",
        })).filter((item) => item.project_id && item.status === "active")
      : [],
    model_aliases: Array.isArray(payload?.model_aliases)
      ? payload.model_aliases.map((item) => ({
          model_alias_id: safeText(item?.model_alias_id, 128),
          alias: safeText(item?.alias, 160),
          display_name: safeText(item?.display_name, 160),
          environment_id: safeText(item?.environment_id, 128),
          status: item?.status === "active" ? "active" : "",
        })).filter((item) => item.model_alias_id && item.environment_id && item.status === "active")
      : [],
  };
}

/** 返回模板表单目录空态；数组每次新建，避免跨身份引用共享。 */
function emptyOptions() {
  return { projects: [], model_aliases: [] };
}

/** 验证列表模板安全投影；非法状态、revision 或 current owner 任一存在都拒绝整包。 */
function validTemplateProjection(value) {
  return Boolean(
    TEMPLATE_ID_PATTERN.test(value?.prompt_template_id)
    && value.name.trim()
    && STATUS_VALUES.has(value.status)
    && validMetadataRevision(value.metadata_revision)
    && VERSION_ID_PATTERN.test(value.current_version_id)
    && positiveInteger(value.current_version_number, 0),
  );
}

/** 验证详情版本历史完整性；current version 必须在同模板不可变历史中精确出现一次。 */
function validDetailProjection(value) {
  if (!validTemplateProjection(value) || !Array.isArray(value.versions) || !value.versions.length) return false;
  const versionIds = value.versions.map((item) => item.prompt_template_version_id);
  const versionNumbers = value.versions.map((item) => item.version_number);
  const versionsValid = value.versions.every((item) => (
    VERSION_ID_PATTERN.test(item.prompt_template_version_id)
    && item.prompt_template_id === value.prompt_template_id
    && positiveInteger(item.version_number, 0)
    && item.state === "published"
    && ["all", "selected"].includes(item.project_scope)
    && (item.project_scope !== "selected" || item.project_ids.length > 0)
  ));
  const current = value.versions.find((item) => item.prompt_template_version_id === value.current_version_id);
  return Boolean(
    versionsValid
    && new Set(versionIds).size === versionIds.length
    && new Set(versionNumbers).size === versionNumbers.length
    && current
    && current.version_number === value.current_version_number,
  );
}

/** 验证解密定义白名单；变量 schema 和 selected 项目必须保持严格、唯一且有界。 */
function validDefinitionProjection(value) {
  const names = Array.isArray(value?.variables) ? value.variables.map((item) => item.name) : [];
  return Boolean(
    TEMPLATE_ID_PATTERN.test(value?.prompt_template_id)
    && VERSION_ID_PATTERN.test(value?.prompt_template_version_id)
    && positiveInteger(value?.version_number, 0)
    && typeof value.content === "string"
    && value.content.length > 0
    && ["all", "selected"].includes(value.project_scope)
    && (value.project_scope !== "selected" || value.project_ids.length > 0)
    && validVariables(value.variables)
    && new Set(names).size === names.length
    && value.change_summary.trim().length > 0,
  );
}

/** 合并 mutation 响应到详情；发布时追加精确版本元数据，PATCH 只更新模板投影。 */
function mergeMutationDetail(current, template, version, needsVersion) {
  const existingVersions = current?.prompt_template_id === template.prompt_template_id
    ? current.versions.map(copyVersion)
    : [];
  const versions = needsVersion
    ? [version, ...existingVersions.filter(
        (item) => item.prompt_template_version_id !== version.prompt_template_version_id,
      )]
    : existingVersions;
  return { ...template, versions };
}

/**
 * 复制 dialog 给当前 renderer/hydration。
 *
 * 只暴露当前页面同步所需草稿；controller、owner、fingerprint 和稳定 request_id 永不公开或持久化。
 */
function copyDialog(value) {
  return {
    mode: value.mode,
    templateId: value.templateId,
    baseMetadataRevision: value.baseMetadataRevision,
    draft: cloneDraft(value.draft),
    dirty: value.dirty,
    busy: value.busy,
    conflicted: value.conflicted,
    sideLatest: value.sideLatest ? copyDetail(value.sideLatest) : null,
    actionError: value.actionError ? { ...value.actionError } : null,
    feedback: value.feedback,
  };
}

/** 复制唯一有界文本数组；顺序是发布语义的一部分，因此仅去重不排序。 */
function uniqueTextArray(value, maximumLength, maximumItems) {
  if (!Array.isArray(value)) return [];
  const normalized = value.slice(0, maximumItems).map((item) => safeText(item, maximumLength));
  return [...new Set(normalized.filter(Boolean))];
}

/** metadata revision 只接受正安全整数，禁止和 version_number 的 fallback 混用。 */
function validMetadataRevision(value) {
  return Number.isSafeInteger(Number(value)) && Number(value) >= 1 ? Number(value) : null;
}

/** 正整数规范化用于版本号；非法值使用调用方显式 fallback。 */
function positiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : fallback;
}

/** 非负整数规范化用于分页和计数；异常值不扩大客户端资源边界。 */
function nonnegativeInteger(value, fallback) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : fallback;
}

/** 有界整数规范化用于变量长度和分页上限。 */
function boundedInteger(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= minimum && number <= maximum ? number : fallback;
}

/** 构造响应完整性错误，不包含 DTO、正文或 tenant owner。 */
function integrityError() {
  return { status: 500, code: "prompt_template.integrity_failed", message: "模板响应完整性异常" };
}

/** 统一错误投影只保留公开 code/message/request/trace，原始异常和未知 details 全部丢弃。 */
function publicError(error, fallbackCode = "prompt_template.unavailable", fallbackMessage = "Prompt 模板暂时不可用") {
  return {
    status: Number(error?.status ?? 0),
    code: safeText(error?.code, 128) || fallbackCode,
    message: safeText(error?.message, 240) || fallbackMessage,
    requestId: safeText(error?.requestId ?? error?.request_id, 256) || "-",
    traceId: safeText(error?.traceId ?? error?.trace_id, 256) || "-",
  };
}

/** 裁剪展示文本并移除控制字符；正文 content 不调用本 helper，以免误把裁剪后的正文当权威定义。 */
function safeText(value, maximum) {
  return String(value ?? "").replace(/[\u0000-\u001f\u007f-\u009f]/g, "").slice(0, maximum);
}
