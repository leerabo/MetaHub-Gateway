import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";

/** PAGE-093 的浏览器内存上限；后端会独立重复校验，不能把这些值当作安全边界。 */
const DEFAULT_LIMITS = Object.freeze({
  max_history_messages: 20,
  max_message_chars: 8000,
  max_history_bytes: 65536,
  session_ttl_seconds: 28800,
});

/** 历史容量耗尽时只投影固定提示；后端仍须独立校验消息数、UTF-8 字节数和权限边界。 */
const HISTORY_LIMIT_MESSAGE = "当前会话已达到上下文上限，请清空或新建会话";

/**
 * 创建不继承 ``Object.prototype`` 的模板变量容器。
 *
 * 变量名虽然已由后端和 options 净化器拒绝原型保留键，前端仍不能依赖普通对象、展开语法或
 * ``Object.fromEntries`` 保存客户输入；独立的 null-prototype 容器可阻断未来合同漂移导致的
 * ``__proto__`` 赋值副作用。调用方必须逐个写入已经过声明校验的变量名。
 */
function createTemplateVariableMap() {
  return Object.create(null);
}

/** 逐键复制模板变量值，同时保持 null-prototype；返回值只供当前页面同步使用。 */
function copyTemplateVariableMap(source) {
  const result = createTemplateVariableMap();
  for (const [name, value] of Object.entries(source ?? {})) result[name] = value;
  return result;
}

/** 从实时认证快照构造企业 Chat 的完整授权 owner。 */
export function enterpriseChatIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.CHAT_USE);
}

/** 从实时认证快照构造模板调用附加 owner；缺少 use 不影响普通 Chat owner。 */
export function enterpriseChatTemplateUseIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PROMPT_TEMPLATE_USE);
}

/**
 * 创建企业 Chat 私有状态机。
 *
 * 正文、草稿、AbortController 和操作 owner 全部封装在闭包中；snapshot 仅供当前页面同步渲染，调用方
 * 不得写入 Storage、URL、遥测或 console。身份、权限、导航或 Session revision 漂移会推进代次并清空正文。
 */
export function createEnterpriseChatPageState() {
  let identity = "";
  let templateUseIdentity = "";
  let navigation = "";
  let pageGeneration = 0;
  let requestGeneration = 0;
  let operation = null;
  let options = null;
  let selectedProjectId = "";
  let selectedEnvironmentId = "";
  let selectedModelAliasId = "";
  let selectedPromptTemplateVersionId = "";
  let templateVariableValues = createTemplateVariableMap();
  let templateOptionsStale = false;
  let session = null;
  let messages = [];
  let draft = "";
  let status = "idle";
  let error = null;

  /** 推进页面代次并取消在途请求；旧回调即使忽略 AbortSignal 也无法通过 owner 比较。 */
  function invalidateOperation() {
    pageGeneration += 1;
    operation?.controller.abort();
    operation = null;
  }

  /** 清除全部正文与安全摘要，供离页、注销、401/403、pagehide 和身份漂移共同调用。 */
  function clear() {
    invalidateOperation();
    identity = "";
    templateUseIdentity = "";
    navigation = "";
    requestGeneration = 0;
    options = null;
    selectedProjectId = "";
    selectedEnvironmentId = "";
    selectedModelAliasId = "";
    selectedPromptTemplateVersionId = "";
    templateVariableValues = createTemplateVariableMap();
    templateOptionsStale = false;
    session = null;
    messages = [];
    draft = "";
    status = "idle";
    error = null;
  }

  /**
   * 同步后端授权 owner；只有 ``enterprise-chat`` 导航和 ``chat.use`` 同时存在时才建立页面身份。
   * 返回 false 表示调用方不得预取或渲染受保护 DOM。
   */
  function syncOwner(authContext, activeNavigationItemId) {
    const nextIdentity = enterpriseChatIdentitySnapshot(authContext);
    const nextTemplateUseIdentity = enterpriseChatTemplateUseIdentitySnapshot(authContext);
    const nextNavigation = activeNavigationItemId === "enterprise-chat" ? "enterprise-chat" : "";
    if (!nextIdentity || !nextNavigation) {
      clear();
      return false;
    }
    if (
      identity !== nextIdentity
      || templateUseIdentity !== nextTemplateUseIdentity
      || navigation !== nextNavigation
    ) {
      clear();
      identity = nextIdentity;
      templateUseIdentity = nextTemplateUseIdentity;
      navigation = nextNavigation;
      pageGeneration += 1;
    }
    return true;
  }

  /** 创建一次严格绑定身份、页面代次、会话 ID/revision 的异步操作票据。 */
  function begin(kind, extra = {}) {
    if (!identity || navigation !== "enterprise-chat" || operation) return null;
    requestGeneration += 1;
    const controller = new AbortController();
    operation = Object.freeze({
      kind,
      identity,
      navigation,
      pageGeneration,
      requestGeneration,
      chatSessionId: session?.chat_session_id ?? "",
      sessionRevision: Number(session?.state_revision ?? 0),
      controller,
      signal: controller.signal,
      ...extra,
    });
    error = null;
    return operation;
  }

  /** 比较当前身份、代次和会话 revision，淘汰旧租户或旧会话的迟到响应。 */
  function isCurrent(ticket) {
    return Boolean(
      ticket
      && operation === ticket
      && !ticket.signal.aborted
      && ticket.identity === identity
      && ticket.navigation === navigation
      && ticket.pageGeneration === pageGeneration
      && ticket.requestGeneration === requestGeneration
      && ticket.chatSessionId === (session?.chat_session_id ?? "")
      && ticket.sessionRevision === Number(session?.state_revision ?? 0),
    );
  }

  /** 仅清理当前票据，迟到 finally 不能覆盖较新操作。 */
  function finish(ticket) {
    if (operation === ticket) operation = null;
  }

  /** 开始读取安全选项目录。 */
  function beginOptions() {
    const ticket = begin("options");
    if (ticket) status = "loading";
    return ticket;
  }

  /** 接受严格选项目录并选择第一个可用项目/环境和同环境别名。 */
  function acceptOptions(ticket, payload) {
    if (!isCurrent(ticket)) return false;
    const previousTemplateVersionId = selectedPromptTemplateVersionId;
    options = sanitizeOptions(payload, Boolean(templateUseIdentity));
    const readyProject = options.projects.find((item) => item.availability === "ready") ?? null;
    selectedProjectId = readyProject?.project_id ?? "";
    selectedEnvironmentId = readyProject?.environment_id ?? "";
    selectedModelAliasId = options.model_aliases.find(
      (item) => item.environment_id === selectedEnvironmentId,
    )?.model_alias_id ?? "";
    if (previousTemplateVersionId) {
      const refreshed = options.prompt_templates.find(
        (item) => item.prompt_template_version_id === previousTemplateVersionId,
      );
      // 失效模板不能静默降级成无模板；保留不可发送的显式选择，直到用户重选或选择“无模板”。
      selectedPromptTemplateVersionId = previousTemplateVersionId;
      templateOptionsStale = !refreshed;
      if (!refreshed) templateVariableValues = createTemplateVariableMap();
    }
    status = "ready";
    error = null;
    finish(ticket);
    return true;
  }

  /** 切换项目/环境会终止旧会话并清空正文，不能把上一资源上下文继续发送到新模型。 */
  function selectProject(projectId, environmentId) {
    if (operation || !options) return false;
    const candidate = options.projects.find((item) => (
      item.project_id === projectId
      && item.environment_id === environmentId
      && item.availability === "ready"
    ));
    if (!candidate) return false;
    invalidateConversation();
    clearTemplateSelection();
    selectedProjectId = candidate.project_id;
    selectedEnvironmentId = candidate.environment_id;
    selectedModelAliasId = options.model_aliases.find(
      (item) => item.environment_id === selectedEnvironmentId,
    )?.model_alias_id ?? "";
    return true;
  }

  /** 切换模型别名同样清除会话与正文，后端建会话时仍重新复核 active 和 tenant owner。 */
  function selectModelAlias(modelAliasId) {
    if (operation || !options) return false;
    const candidate = options.model_aliases.find((item) => (
      item.model_alias_id === modelAliasId && item.environment_id === selectedEnvironmentId
    ));
    if (!candidate) return false;
    invalidateConversation();
    selectedModelAliasId = candidate.model_alias_id;
    return true;
  }

  /**
   * 显式选择模板 current version 或“无模板”。
   *
   * 不适用于当前项目的模板失败关闭；选择变化只清变量值，不自动修改模型别名或自由草稿。
   */
  function selectPromptTemplate(promptTemplateVersionId) {
    if (operation || !options) return false;
    const versionId = String(promptTemplateVersionId ?? "");
    if (!versionId) {
      clearTemplateSelection();
      return true;
    }
    if (!templateUseIdentity) return false;
    const candidate = options.prompt_templates.find((item) => (
      item.prompt_template_version_id === versionId
      && templateAppliesToProject(item, selectedProjectId)
    ));
    if (!candidate) return false;
    selectedPromptTemplateVersionId = candidate.prompt_template_version_id;
    templateVariableValues = createTemplateVariableMap();
    templateOptionsStale = false;
    error = null;
    return true;
  }

  /** 更新一个已声明模板变量；值只驻留闭包并按该变量 max_chars 截断。 */
  function setTemplateVariable(name, value) {
    if (operation || templateOptionsStale) return false;
    const selected = selectedPromptTemplate();
    const variable = selected?.variables.find((item) => item.name === name);
    if (!variable) return false;
    const nextValues = copyTemplateVariableMap(templateVariableValues);
    nextValues[variable.name] = String(value ?? "").slice(0, variable.max_chars);
    templateVariableValues = nextValues;
    error = null;
    return true;
  }

  /** 创建会话票据；资源 ID 只来自已接受的后端 options。 */
  function beginCreateSession() {
    if (session || !selectedProjectId || !selectedEnvironmentId || !selectedModelAliasId) return null;
    const ticket = begin("create", {
      projectId: selectedProjectId,
      environmentId: selectedEnvironmentId,
      modelAliasId: selectedModelAliasId,
    });
    if (ticket) status = "creating";
    return ticket;
  }

  /** 接受会话安全摘要，不接受 Virtual Key、grant 或正文扩展字段。 */
  function acceptSession(ticket, payload) {
    if (!isCurrent(ticket) || ticket.kind !== "create") return false;
    const next = sanitizeSession(payload?.session);
    if (
      !next
      || next.project_id !== selectedProjectId
      || next.environment_id !== selectedEnvironmentId
      || next.model_alias_id !== selectedModelAliasId
    ) return reject(ticket, publicError(null, "chat.response_invalid", "会话响应无效"));
    session = next;
    // 新 ChatSession 重新冻结模板 owner；旧变量值不能跨 Session 沿用。
    templateVariableValues = createTemplateVariableMap();
    status = "ready";
    error = null;
    finish(ticket);
    return true;
  }

  /** 草稿只写当前闭包；到达后端限制时截断，不能进入错误、URL 或 Storage。 */
  function setDraft(value) {
    draft = String(value ?? "").slice(0, limits().max_message_chars);
    return snapshot();
  }

  /**
   * 冻结单次发送所需 history。
   * 返回的 ticket 含正文，仅允许调用方立即传给 API client；它不会出现在 snapshot 或错误对象中。
   */
  function beginSend(clientMessageId) {
    const selectedTemplate = selectedPromptTemplate();
    const templateMode = Boolean(selectedPromptTemplateVersionId);
    const content = draft;
    // uncertain 表示上一次调用可能已经计费但响应不可恢复；必须先清空，禁止生成新幂等 ID 二次调用。
    if (!session || operation || status !== "ready") return null;
    if (templateMode) {
      if (
        !templateUseIdentity
        || templateOptionsStale
        || !selectedTemplate
        || !templateAppliesToProject(selectedTemplate, selectedProjectId)
        || !validTemplateVariables(selectedTemplate, templateVariableValues)
      ) return null;
      const history = messages.map((item) => ({ role: item.role, content: item.content }));
      if (!validTemplateHistory(history, limits())) return null;
      const templateVariables = createTemplateVariableMap();
      for (const item of selectedTemplate.variables) {
        templateVariables[item.name] = templateVariableValues[item.name] ?? "";
      }
      const ticket = begin("send", {
        clientMessageId,
        history,
        promptTemplateVersionId: selectedTemplate.prompt_template_version_id,
        templateVariables: Object.freeze(templateVariables),
        templateOptionsRevision: options.options_revision,
        templateUseIdentity,
      });
      if (ticket) status = "sending";
      return ticket;
    }
    if (!isValidDraft(content)) return null;
    const history = [
      ...messages.map((item) => ({ role: item.role, content: item.content })),
      { role: "user", content },
    ];
    if (!validHistory(history, limits())) return null;
    const ticket = begin("send", { clientMessageId, history });
    if (ticket) status = "sending";
    return ticket;
  }

  /** 成功响应只接收当前 Session 的文本回复和固定治理摘要。 */
  function acceptMessage(ticket, payload) {
    if (!isCurrent(ticket) || ticket.kind !== "send") return false;
    const assistant = String(payload?.assistant?.content ?? "");
    const renderedUser = ticket.promptTemplateVersionId
      ? String(payload?.rendered_user ?? "")
      : String(ticket.history[ticket.history.length - 1]?.content ?? "");
    if (
      payload?.chat_session_id !== session.chat_session_id
      || payload?.client_message_id !== ticket.clientMessageId
      || !assistant
      || !renderedUser
      || new TextEncoder().encode(renderedUser).byteLength > limits().max_history_bytes
      || !Number.isSafeInteger(Number(payload?.session_revision))
      || Number(payload.session_revision) <= Number(session.state_revision)
    ) return reject(ticket, publicError(null, "chat.response_invalid", "模型响应无效"));
    messages = [
      ...messages,
      { role: "user", content: renderedUser, execution: null },
      { role: "assistant", content: assistant, execution: sanitizeExecution(payload.execution) },
    ];
    draft = "";
    if (ticket.promptTemplateVersionId) {
      templateVariableValues = createTemplateVariableMap();
    }
    session = { ...session, state_revision: Number(payload.session_revision) };
    status = "ready";
    error = null;
    finish(ticket);
    return true;
  }

  /** 开始清空；存在发送时固定拒绝，本地 abort 不能冒充后端取消成功。 */
  function beginClearSession() {
    if (!session || operation) return null;
    const ticket = begin("clear");
    if (ticket) status = "clearing";
    return ticket;
  }

  /** 204 只在完整 owner 仍匹配时清除正文；迟到清空响应不能抹掉新会话。 */
  function acceptClear(ticket) {
    if (!isCurrent(ticket) || ticket.kind !== "clear") return false;
    finish(ticket);
    invalidateConversation();
    status = "ready";
    return true;
  }

  /**
   * 保存固定公开错误。
   * 401/403 是强身份边界，立即销毁全部正文；其他错误不包含原始异常字符串或请求正文。
   */
  function reject(ticket, candidate) {
    if (!isCurrent(ticket)) return false;
    // 即使候选已经是 AuthApiError，也必须重新投影固定五字段；Error 的 name、stack、diagnosis、
    // suggestion 或未来扩展字段都不能进入页面状态，更不能意外携带 URL、Provider 或正文。
    const nextError = publicError(candidate);
    finish(ticket);
    if (Number(candidate?.status) === 401 || Number(candidate?.status) === 403) {
      clear();
      return true;
    }
    const staleTemplateCodes = new Set([
      "prompt_template.disabled",
      "prompt_template.version_conflict",
      "prompt_template.not_applicable",
      "prompt_template.not_found",
    ]);
    // 模板失效后保留显式选择但清空变量并标记目录 stale，禁止静默按自由 draft 发送。
    if (ticket.kind === "send" && staleTemplateCodes.has(nextError.code)) {
      templateVariableValues = createTemplateVariableMap();
      templateOptionsStale = true;
      status = "ready";
      error = nextError;
      return true;
    }
    // 可修正的 422 保留模板和变量值，不进入 uncertain，也不销毁普通 ChatSession。
    if (ticket.kind === "send" && nextError.code === "prompt_template.variables_invalid") {
      status = "ready";
      error = nextError;
      return true;
    }
    const sessionInvalid = new Set([
      "chat.session_not_found",
      "chat.session_expired",
      "chat.session_cleared",
      "chat.project_unavailable",
      "chat.model_unavailable",
      "chat.virtual_key_unavailable",
    ]).has(nextError.code);
    // 发送期间确认资源失效时必须销毁整段正文，防止旧 Session 在权限或配置变化后继续使用。
    if (ticket.kind === "send" && sessionInvalid) {
      invalidateConversation();
      error = nextError;
      return true;
    }
    if (
      ticket.kind === "send"
      && (nextError.status >= 500 || nextError.code === "chat.message_duplicate")
    ) {
      // 5xx/重复幂等可能发生在 Provider 已执行之后；不保存 Response 时无法安全续写，只允许用户清空。
      status = "uncertain";
      error = nextError;
      return true;
    }
    status = session ? "ready" : (options ? "ready" : "error");
    error = nextError;
    return true;
  }

  /** 当前页面含正文、会话或在途请求时阻止 15 秒全页重建 DOM。 */
  function blocksRefresh() {
    return Boolean(
      operation
      || session
      || messages.length
      || draft
      || selectedPromptTemplateVersionId
      || Object.keys(templateVariableValues).length,
    );
  }

  /** 返回当前同步渲染快照；AbortController、Token、owner、history ticket 不会被投影。 */
  function snapshot() {
    const currentLimits = limits();
    const historyLimitReached = reachesHistoryLimit(messages, draft, currentLimits);
    return Object.freeze({
      status,
      options,
      selectedProjectId,
      selectedEnvironmentId,
      selectedModelAliasId,
      selectedPromptTemplateVersionId,
      templateVariableValues: copyTemplateVariableMap(templateVariableValues),
      templateOptionsStale,
      templateUseAuthorized: Boolean(templateUseIdentity),
      templateReady: templateSelectionReady(),
      session: session ? { ...session } : null,
      messages: messages.map((item) => ({
        role: item.role,
        content: item.content,
        execution: item.execution ? { ...item.execution } : null,
      })),
      draft,
      error: error ? { ...error } : null,
      busy: Boolean(operation),
      operationKind: operation?.kind ?? "",
      // 只暴露派生布尔值和固定文案，绝不把正文、累计字节或后端权威判断塞进提示。
      historyLimitReached,
      historyLimitMessage: historyLimitReached ? HISTORY_LIMIT_MESSAGE : "",
      limits: currentLimits,
    });
  }

  /** 选择变化时同步丢弃会话和正文，并推进代次淘汰所有旧响应。 */
  function invalidateConversation() {
    invalidateOperation();
    session = null;
    messages = [];
    draft = "";
    templateVariableValues = createTemplateVariableMap();
    error = null;
    status = options ? "ready" : "idle";
  }

  /** 清除模板选择和变量值；仅由显式“无模板”、项目切换或授权边界调用。 */
  function clearTemplateSelection() {
    selectedPromptTemplateVersionId = "";
    templateVariableValues = createTemplateVariableMap();
    templateOptionsStale = false;
  }

  /** 返回当前 options 中同一 current version；stale 哨兵不会被误当成可调用模板。 */
  function selectedPromptTemplate() {
    if (!selectedPromptTemplateVersionId || templateOptionsStale || !options) return null;
    return options.prompt_templates.find(
      (item) => item.prompt_template_version_id === selectedPromptTemplateVersionId,
    ) ?? null;
  }

  /** 判断模板是否适用于当前项目；推荐模型不参与硬约束。 */
  function templateAppliesToProject(template, projectId) {
    return template?.project_scope === "all"
      || template?.applicable_project_ids?.includes(projectId) === true;
  }

  /** 判断选择、权限、适用范围及变量 exact-set 是否可形成模板 send ticket。 */
  function templateSelectionReady() {
    const template = selectedPromptTemplate();
    return Boolean(
      template
      && templateUseIdentity
      && !templateOptionsStale
      && templateAppliesToProject(template, selectedProjectId)
      && validTemplateVariables(template, templateVariableValues),
    );
  }

  /** 后端 limits 缺失或非法时使用更保守的固定默认值。 */
  function limits() {
    return { ...DEFAULT_LIMITS, ...(options?.limits ?? {}) };
  }

  return Object.freeze({
    syncOwner,
    clear,
    beginOptions,
    acceptOptions,
    selectProject,
    selectModelAlias,
    selectPromptTemplate,
    setTemplateVariable,
    beginCreateSession,
    acceptSession,
    setDraft,
    beginSend,
    acceptMessage,
    beginClearSession,
    acceptClear,
    reject,
    blocksRefresh,
    snapshot,
  });
}

/** 净化 options 白名单；未知字段永远不会进入页面状态。 */
function sanitizeOptions(payload, includePromptTemplates) {
  const limits = payload?.limits ?? {};
  const promptTemplates = includePromptTemplates && Array.isArray(payload?.prompt_templates)
    ? payload.prompt_templates.map(sanitizePromptTemplateOption).filter(Boolean)
    : [];
  const uniquePromptTemplates = new Set(promptTemplates.map(
    (item) => item.prompt_template_version_id,
  )).size === promptTemplates.length ? promptTemplates : [];
  return Object.freeze({
    options_revision: safeText(payload?.options_revision, 128),
    projects: Array.isArray(payload?.projects) ? payload.projects.map((item) => Object.freeze({
      project_id: safeText(item?.project_id, 128),
      name: safeText(item?.name, 160),
      environment_id: safeText(item?.environment_id, 128),
      environment_name: safeText(item?.environment_name, 160),
      availability: ["ready", "key_unavailable", "ambiguous_key"].includes(item?.availability)
        ? item.availability
        : "key_unavailable",
    })).filter((item) => item.project_id && item.environment_id) : [],
    model_aliases: Array.isArray(payload?.model_aliases) ? payload.model_aliases.map((item) => Object.freeze({
      model_alias_id: safeText(item?.model_alias_id, 128),
      alias: safeText(item?.alias, 160),
      display_name: safeText(item?.display_name, 160),
      environment_id: safeText(item?.environment_id, 128),
    })).filter((item) => item.model_alias_id && item.environment_id) : [],
    // 只有实时 prompt_template.use owner 存在时才持有最小调用投影；read/write 权限不能替代 use。
    prompt_templates: Object.freeze(uniquePromptTemplates),
    limits: Object.freeze({
      max_history_messages: boundedInteger(limits.max_history_messages, 1, 20, 20),
      max_message_chars: boundedInteger(limits.max_message_chars, 1, 8000, 8000),
      max_history_bytes: boundedInteger(limits.max_history_bytes, 1, 65536, 65536),
      session_ttl_seconds: boundedInteger(limits.session_ttl_seconds, 60, 28800, 28800),
    }),
  });
}

/** 净化 Chat 模板最小投影；正文、描述、历史、tenant 和完整资源目录永不进入 PAGE-093。 */
function sanitizePromptTemplateOption(value) {
  const source = value && typeof value === "object" ? value : {};
  const templateId = safeText(source.prompt_template_id, 128);
  const versionId = safeText(source.prompt_template_version_id, 128);
  const versionNumber = Number(source.version_number);
  const variables = Array.isArray(source.variables)
    ? source.variables.slice(0, 32).map((item) => Object.freeze({
        name: safeText(item?.name, 64),
        label: safeText(item?.label, 80),
        required: item?.required,
        max_chars: Number(item?.max_chars),
      }))
    : [];
  if (
    !/^ptpl_[A-Za-z0-9_-]{1,120}$/.test(templateId)
    || !/^ptv_[A-Za-z0-9_-]{1,120}$/.test(versionId)
    || !Number.isSafeInteger(versionNumber)
    || versionNumber < 1
    || !safeText(source.name, 120).trim()
    || !["all", "selected"].includes(source.project_scope)
    || new Set(variables.map((item) => item.name)).size !== variables.length
    || variables.some((item) => (
      !/^[a-z][a-z0-9_]{0,63}$/.test(item.name)
      || ["constructor", "prototype", "__proto__"].includes(item.name)
      || !item.label.trim()
      || typeof item.required !== "boolean"
      || !Number.isSafeInteger(item.max_chars)
      || item.max_chars < 1
      || item.max_chars > 8000
    ))
  ) return null;
  return Object.freeze({
    prompt_template_id: templateId,
    prompt_template_version_id: versionId,
    version_number: versionNumber,
    name: safeText(source.name, 120),
    variables: Object.freeze(variables),
    project_scope: source.project_scope,
    applicable_project_ids: Object.freeze(uniqueSafeIds(source.applicable_project_ids, 100)),
    recommended_model_alias_ids: Object.freeze(uniqueSafeIds(source.recommended_model_alias_ids, 8)),
  });
}

/** 净化会话安全摘要，拒绝缺失关键 owner ID 或 revision 的响应。 */
function sanitizeSession(value) {
  const stateRevision = Number(value?.state_revision);
  const result = {
    chat_session_id: safeText(value?.chat_session_id, 128),
    project_id: safeText(value?.project_id, 128),
    project_name: safeText(value?.project_name, 160),
    environment_id: safeText(value?.environment_id, 128),
    environment_name: safeText(value?.environment_name, 160),
    model_alias_id: safeText(value?.model_alias_id, 128),
    model_alias: safeText(value?.model_alias, 160),
    state_revision: Number.isSafeInteger(stateRevision) && stateRevision >= 1 ? stateRevision : 0,
    expires_at: safeText(value?.expires_at, 64),
  };
  // 后端绑定字段任一缺失都拒绝建立会话，避免用不完整租户资源关系进入后续发送流程。
  return result.chat_session_id && result.project_id && result.environment_id
    && result.model_alias_id && result.state_revision ? result : null;
}

/** 只保留治理摘要固定字段；任何 Provider、模型内部 ID、Endpoint 或原始错误均被丢弃。 */
function sanitizeExecution(value) {
  return Object.freeze({
    request_id: safeText(value?.request_id, 256) || "-",
    trace_id: safeText(value?.trace_id, 256) || "-",
    input_tokens: nonNegativeInteger(value?.input_tokens),
    output_tokens: nonNegativeInteger(value?.output_tokens),
    total_tokens: nonNegativeInteger(value?.total_tokens),
    cache_tokens: nonNegativeInteger(value?.cache_tokens),
    cost_cny: /^\d+(?:\.\d{1,6})?$/.test(String(value?.cost_cny ?? ""))
      ? String(value.cost_cny)
      : "0.000000",
    policy: Object.freeze({
      decision: safeText(value?.policy?.decision, 64) || "unknown",
      reason_code: safeText(value?.policy?.reason_code, 128),
    }),
    fallback_triggered: value?.fallback_triggered === true,
    cache_status: ["hit", "miss", "not_configured", "unknown"].includes(value?.cache_status)
      ? value.cache_status
      : "unknown",
  });
}

/** 验证单条草稿；这里只改善交互，后端仍是权威输入边界。 */
function isValidDraft(value) {
  return typeof value === "string" && value.trim().length > 0;
}

/** 校验角色交替、字符数和 UTF-8 总字节数，避免前端误发明显无效正文。 */
function validHistory(history, limits) {
  if (!Array.isArray(history) || history.length < 1 || history.length > limits.max_history_messages) return false;
  let bytes = 0;
  for (let index = 0; index < history.length; index += 1) {
    const item = history[index];
    const expectedRole = index % 2 === 0 ? "user" : "assistant";
    // 角色必须严格交替且单条正文受限，防止客户端构造越界历史绕过后端治理输入约束。
    if (item.role !== expectedRole || !item.content.trim() || item.content.length > limits.max_message_chars) return false;
    bytes += new TextEncoder().encode(item.content).byteLength;
  }
  return history[history.length - 1].role === "user" && bytes <= limits.max_history_bytes;
}

/**
 * 校验模板模式既有 history。
 *
 * 浏览器只提交空数组或完整 user/assistant 对；Gateway 渲染后再追加本轮 user，因此这里为追加消息预留一项。
 */
function validTemplateHistory(history, limits) {
  if (!Array.isArray(history) || history.length % 2 !== 0 || history.length >= limits.max_history_messages) {
    return false;
  }
  let bytes = 0;
  for (let index = 0; index < history.length; index += 1) {
    const item = history[index];
    const expectedRole = index % 2 === 0 ? "user" : "assistant";
    // 模板 history 必须是完整对且正文非空；渲染后的本轮 user 只能由 Gateway 追加。
    if (item.role !== expectedRole || !item.content.trim()) return false;
    bytes += new TextEncoder().encode(item.content).byteLength;
  }
  return bytes < limits.max_history_bytes;
}

/** 模板变量必须与声明 exact-set 一致；空选填值仍显式提交，服务端会再次做 UTF-8 上限校验。 */
function validTemplateVariables(template, values) {
  if (!template || !values || typeof values !== "object" || Array.isArray(values)) return false;
  const expectedNames = template.variables.map((item) => item.name);
  const actualNames = Object.keys(values);
  if (actualNames.some((name) => !expectedNames.includes(name))) return false;
  return template.variables.every((item) => {
    const value = String(values[item.name] ?? "");
    return value.length <= item.max_chars && (!item.required || value.trim().length > 0);
  });
}

/**
 * 判断当前内存历史或“历史 + 草稿”是否已无法再安全发送。
 *
 * 空草稿不计作新消息，便于用户在剩余容量内继续输入；一旦现有历史本身已经达到消息数或字节上限，
 * 则立即失败关闭。字节按 UTF-8 计算，与后端合同一致，但该派生结果只用于交互禁用，不能替代后端校验。
 */
function reachesHistoryLimit(messages, draft, limits) {
  const history = Array.isArray(messages) ? messages : [];
  if (history.length >= limits.max_history_messages) return true;
  const historyBytes = history.reduce(
    (total, item) => total + new TextEncoder().encode(String(item?.content ?? "")).byteLength,
    0,
  );
  if (historyBytes >= limits.max_history_bytes) return true;
  if (!isValidDraft(draft)) return false;
  return historyBytes + new TextEncoder().encode(draft).byteLength > limits.max_history_bytes;
}

/** 构造固定公开错误，禁止字符串化原始异常以免 URL 或正文进入可见状态。 */
function publicError(error, fallbackCode = "chat.gateway_unavailable", fallbackMessage = "企业 Chat 暂时不可用") {
  return Object.freeze({
    status: Number(error?.status ?? 0),
    code: safeText(error?.code, 128) || fallbackCode,
    message: safeText(error?.message, 240) || fallbackMessage,
    requestId: safeText(error?.requestId ?? error?.request_id, 256) || "-",
    traceId: safeText(error?.traceId ?? error?.trace_id, 256) || "-",
  });
}

/** 裁剪可显示短文本并移除控制字符，防止异常字段破坏布局。 */
function safeText(value, maxLength) {
  return String(value ?? "").replace(/[\u0000-\u001f\u007f-\u009f]/g, "").slice(0, maxLength);
}

/** 把后端整数限制收敛到本期最大边界，拒绝异常扩大页面内存。 */
function boundedInteger(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= minimum && number <= maximum ? number : fallback;
}

/** Token 计数只允许非负安全整数。 */
function nonNegativeInteger(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : 0;
}

/** 复制有界安全资源 ID 数组；保持发布顺序，只去重，不把推荐模型解释成路由白名单。 */
function uniqueSafeIds(value, maximumItems) {
  if (!Array.isArray(value)) return [];
  const normalized = value.slice(0, maximumItems)
    .map((item) => safeText(item, 128))
    .filter((item) => /^[A-Za-z0-9_-]{1,128}$/.test(item));
  return [...new Set(normalized)];
}
