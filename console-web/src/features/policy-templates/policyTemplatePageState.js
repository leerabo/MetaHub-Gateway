import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";
import { createPolicyTemplateRequestId } from "../../lib/policyTemplates.js";

/** 五个内置模板路径 ID 是数据库 seed 与 API 的稳定公开合同。 */
const TEMPLATE_ID_PATTERN = /^ptpl_[A-Za-z0-9_-]{1,120}$/;
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;
const HASH_PATTERN = /^[0-9a-f]{64}$/;
const APPLICATION_ID_PATTERN = /^ptapp_[0-9a-f]{32}$/;
const POLICY_ID_PATTERN = /^pol_[0-9a-f]{32}$/;
const SNAPSHOT_ID_PATTERN = /^psnap_[0-9a-f]{32}$/;
const TEMPLATE_CODES = new Set([
  "low_cost_mode",
  "high_security_mode",
  "domestic_models_only",
  "budget_burst_guard",
  "customer_service_redaction",
]);

/** 从实时认证快照构造 PAGE-095 读取 owner，角色名不能替代精确 permission。 */
export function policyTemplateReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.POLICY_TEMPLATE_READ);
}

/** 从实时认证快照构造 PAGE-095 应用 owner，读取与关键命令授权必须分离。 */
export function policyTemplateApplyIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.POLICY_TEMPLATE_APPLY);
}

/**
 * 创建 PAGE-095 私有状态机。
 *
 * catalog、preview、apply 三条异步 lane 各自持有 AbortController 和 generation。preview nonce、
 * 双 CAS 与写 request_id 只存在于闭包；离页、注销、换身份或刷新代次变化会同步销毁。
 */
export function createPolicyTemplatePageState({
  requestIdFactory = createPolicyTemplateRequestId,
} = {}) {
  let readIdentity = "";
  let applyIdentity = "";
  let navigation = "";
  let refreshGeneration = 0;
  let pageGeneration = 0;
  let templates = [];
  let selectedTemplateId = "";
  let bindings = emptyBindings();
  let preview = null;
  let confirmation = null;
  let result = null;
  let pageError = null;
  const lanes = {
    catalog: createLane(),
    preview: createLane(),
    apply: createLane(),
  };

  /** 同步读写授权 owner；任一身份或页面代次改变都先清除旧租户数据和迟到请求。 */
  function syncOwner(owner) {
    const nextReadIdentity = String(owner?.readIdentity ?? "");
    const nextApplyIdentity = String(owner?.applyIdentity ?? "");
    const nextNavigation = owner?.navigation === "policy-templates" ? "policy-templates" : "";
    const nextRefresh = Number(owner?.refreshGeneration);
    if (!nextReadIdentity || !nextNavigation || !Number.isSafeInteger(nextRefresh)) {
      leave();
      return false;
    }
    if (
      readIdentity !== nextReadIdentity
      || applyIdentity !== nextApplyIdentity
      || navigation !== nextNavigation
      || refreshGeneration !== nextRefresh
    ) {
      leave();
      readIdentity = nextReadIdentity;
      applyIdentity = nextApplyIdentity;
      navigation = nextNavigation;
      refreshGeneration = nextRefresh;
      pageGeneration += 1;
    }
    return true;
  }

  /** 开始目录读取；新目录请求淘汰旧 catalog/preview/apply，避免绑定旧资源继续提交。 */
  function beginCatalog() {
    if (!ownedForRead()) return null;
    abortLane("catalog");
    abortLane("preview");
    abortLane("apply");
    preview = null;
    confirmation = null;
    result = null;
    pageError = null;
    return beginLane("catalog", { requestId: requestIdFactory("list") });
  }

  /** 接纳固定五模板目录；未知数量、重复 ID/code 或不安全投影必须整包失败关闭。 */
  function acceptCatalog(ticket, payload) {
    if (!isCurrentLane("catalog", ticket)) return false;
    const source = Array.isArray(payload?.policy_templates) ? payload.policy_templates : [];
    const next = source.map(copyTemplate).filter(Boolean);
    if (
      source.length !== 5
      || next.length !== 5
      || Number(payload?.total) !== 5
      || new Set(next.map((item) => item.policy_template_id)).size !== 5
      || new Set(next.map((item) => item.template_code)).size !== 5
      || next.some((item) => !validCatalogTemplate(item))
    ) {
      rejectLane("catalog", ticket, integrityError());
      return false;
    }
    templates = next;
    if (!templates.some((item) => item.policy_template_id === selectedTemplateId)) {
      selectedTemplateId = templates[0].policy_template_id;
      bindings = defaultBindings(templates[0]);
    }
    finishLane("catalog", ticket);
    pageError = null;
    return true;
  }

  /** 选择目录中的一个模板；切换模板会立即销毁旧预览 nonce、CAS 和 apply 意图。 */
  function selectTemplate(policyTemplateId) {
    const templateId = String(policyTemplateId ?? "");
    const selected = templates.find((item) => item.policy_template_id === templateId);
    if (!selected || lanes.apply.ticket) return false;
    if (templateId === selectedTemplateId) return true;
    abortLane("preview");
    selectedTemplateId = templateId;
    bindings = defaultBindings(selected);
    preview = null;
    confirmation = null;
    result = null;
    pageError = null;
    return true;
  }

  /** 更新一个固定绑定字段；任何变化都使旧精确预览和确认失效。 */
  function setBinding(name, value) {
    const selected = selectedTemplate();
    const field = String(name ?? "");
    if (!selected || lanes.apply.ticket || !selected.required_bindings.includes(field)) return false;
    const normalized = String(value ?? "").trim();
    if (field === "threshold_cny") {
      if (normalized.length > 32) return false;
    } else if (normalized && !RESOURCE_ID_PATTERN.test(normalized)) {
      return false;
    }
    bindings = { ...bindings, [field]: normalized };
    abortLane("preview");
    preview = null;
    confirmation = null;
    result = null;
    pageError = null;
    return true;
  }

  /** 当前绑定达到模板 required_bindings exact-set 时才允许发起精确预览。 */
  function beginPreview() {
    const selected = selectedTemplate();
    if (!ownedForRead() || !selected || !validBindings(selected, bindings) || lanes.apply.ticket) {
      return null;
    }
    abortLane("preview");
    confirmation = null;
    result = null;
    pageError = null;
    return beginLane("preview", {
      requestId: requestIdFactory("preview"),
      policyTemplateId: selected.policy_template_id,
      bindings: Object.freeze({ ...requestBindings(selected, bindings) }),
    });
  }

  /** 接纳精确预览及响应 Header；正文 ID、绑定和双 CAS 任一漂移都不得打开确认。 */
  function acceptPreview(ticket, payload) {
    if (!isCurrentLane("preview", ticket)) return false;
    const source = Array.isArray(payload?.policy_templates) ? payload.policy_templates : [];
    const copied = source.map(copyTemplate);
    const matches = copied.filter(
      (candidate) => candidate?.policy_template_id === ticket.policyTemplateId,
    );
    const item = matches[0];
    const headEtag = String(payload?.headEtag ?? "");
    const candidateEtag = String(payload?.candidateEtag ?? "");
    const ready = ["ready", "ready_with_warnings"].includes(item?.compatibility.status);
    const incompatible = item?.compatibility.status === "incompatible";
    // Candidate CAS 保护的是应用前的当前候选；after hash 是预计写入结果，不能作为 If-Policy-Candidate-Match。
    const expectedCandidateEtag = item?.before_candidate_hash
      ? `"${item.before_candidate_hash}"`
      : "";
    if (
      source.length !== 5
      || Number(payload?.total) !== 5
      || copied.some((candidate) => !candidate || !validCatalogTemplate(candidate))
      || new Set(copied.map((candidate) => candidate.policy_template_id)).size !== 5
      || new Set(copied.map((candidate) => candidate.template_code)).size !== 5
      || matches.length !== 1
      || !item
      || (!ready && !incompatible)
      || (ready && (
        !validReadyPreviewTemplate(item, ticket.bindings)
        || !/^"[1-9][0-9]{0,9}"$/.test(headEtag)
        || !/^"[0-9a-f]{64}"$/.test(candidateEtag)
        || candidateEtag !== expectedCandidateEtag
        || headEtag !== `"${item.expected_head_version}"`
      ))
      || (incompatible && (
        headEtag !== ""
        || candidateEtag !== ""
        || !validIncompatiblePreviewTemplate(item, ticket.bindings)
      ))
    ) {
      rejectLane("preview", ticket, integrityError());
      return false;
    }
    // 只复制当前模板的安全预览字段，不把其他四条目录或未知响应键留在写状态中。
    preview = Object.freeze({
      template: item,
      bindings: Object.freeze({ ...ticket.bindings }),
      headEtag,
      candidateEtag,
    });
    finishLane("preview", ticket);
    pageError = null;
    return true;
  }

  /** 打开应用确认；必须同时具有 apply owner 且后端复合权限结论为 can_apply。 */
  function openConfirmation() {
    if (!ownedForApply() || !preview || !preview.template.compatibility.can_apply) return false;
    confirmation = { reason: "" };
    pageError = null;
    return true;
  }

  /** 更新确认原因；原因不进入 snapshot 之外的任何持久浏览器存储。 */
  function setReason(value) {
    if (!confirmation || lanes.apply.ticket) return false;
    const reason = String(value ?? "");
    if (reason.length > 256 || containsControlCharacter(reason)) return false;
    confirmation = { reason };
    return true;
  }

  /** 关闭确认并销毁原因；已开始 apply 时禁止关闭造成未知结果误重试。 */
  function closeConfirmation() {
    if (!confirmation || lanes.apply.ticket) return false;
    confirmation = null;
    return true;
  }

  /** 冻结完整 apply 意图；同一 ticket 的 request_id 可在网络未知时原样重放。 */
  function beginApply() {
    const reason = String(confirmation?.reason ?? "").trim();
    if (!ownedForApply() || !preview || !reason || lanes.apply.ticket) return null;
    const item = preview.template;
    return beginLane("apply", {
      requestId: requestIdFactory("apply"),
      policyTemplateId: item.policy_template_id,
      templateVersion: item.version,
      previewNonce: item.preview_nonce,
      previewAsOf: item.preview_as_of,
      impactHash: item.impact_hash,
      bindings: preview.bindings,
      reason,
      headEtag: preview.headEtag,
      candidateEtag: preview.candidateEtag,
    });
  }

  /** 接纳成功应用；结果只保留快照和应用安全摘要，并清除一次性 nonce/CAS/原因。 */
  function acceptApply(ticket, payload) {
    if (!isCurrentLane("apply", ticket)) return false;
    const application = payload?.application;
    const snapshot = payload?.policy_snapshot;
    const generatedItems = preview?.template.impact?.evaluation_order.filter(
      (item) => item.generated,
    ) ?? [];
    const expectedSnapshotVersion = Number(preview?.template.impact?.expected_head_version) + 1;
    if (
      !application
      || !snapshot
      || generatedItems.length !== 1
      || application.policy_template_id !== ticket.policyTemplateId
      || application.impact_hash !== ticket.impactHash
      || !TEMPLATE_ID_PATTERN.test(String(application.policy_template_id ?? ""))
      || !APPLICATION_ID_PATTERN.test(String(application.application_id ?? ""))
      || !POLICY_ID_PATTERN.test(String(application.generated_policy_id ?? ""))
      || application.generated_policy_id !== generatedItems[0].policy_id
      || !SNAPSHOT_ID_PATTERN.test(String(snapshot.policy_snapshot_id ?? ""))
      || !Number.isSafeInteger(snapshot.snapshot_version)
      || snapshot.snapshot_version !== expectedSnapshotVersion
    ) {
      rejectLane("apply", ticket, integrityError());
      // 成功响应完整性失败时，旧 nonce/CAS 已进入未知结果状态，必须连同确认原因一起销毁。
      preview = null;
      confirmation = null;
      return false;
    }
    result = Object.freeze({
      templateName: preview.template.name,
      projectName: preview.template.binding_options.projects.find(
        (item) => item.project_id === preview.template.impact.project_id,
      )?.name ?? "所选项目",
      applicationId: String(application.application_id ?? ""),
      policyTemplateId: application.policy_template_id,
      generatedPolicyId: String(application.generated_policy_id ?? ""),
      snapshotId: String(snapshot.policy_snapshot_id),
      snapshotVersion: Number(snapshot.snapshot_version),
      idempotentReplay: payload?.idempotent_replay === true,
      requestId: safeId(payload?.request_id),
      traceId: safeId(payload?.trace_id),
    });
    finishLane("apply", ticket);
    preview = null;
    confirmation = null;
    pageError = null;
    return true;
  }

  /** 记录当前 lane 安全错误；401/403 会由主应用刷新身份，409 要求重新预览。 */
  function reject(name, ticket, error) {
    if (!isCurrentLane(name, ticket)) return false;
    rejectLane(name, ticket, publicError(error));
    if (name === "preview" || name === "apply") {
      preview = null;
      confirmation = null;
    }
    return true;
  }

  /** 关闭已明确显示的结果或错误；不恢复已失效的预览凭证。 */
  function dismissFeedback() {
    if (lanes.apply.ticket) return false;
    result = null;
    pageError = null;
    return true;
  }

  /** 返回当前 DOM 所需的安全副本；nonce、CAS、Token 与完整 owner 永不公开。 */
  function snapshot() {
    const selected = selectedTemplate();
    return Object.freeze({
      authorized: ownedForRead(),
      canApplyPermission: ownedForApply(),
      templates: templates.map((item) => publicTemplate(item)),
      selectedTemplateId,
      selectedTemplate: selected ? publicTemplate(selected) : null,
      bindings: { ...bindings },
      preview: preview ? publicPreview(preview) : null,
      confirmation: confirmation ? { reason: confirmation.reason } : null,
      result,
      pageError,
      loadingCatalog: Boolean(lanes.catalog.ticket),
      loadingPreview: Boolean(lanes.preview.ticket),
      applying: Boolean(lanes.apply.ticket),
    });
  }

  /** 离开页面并销毁全部当前租户状态、AbortController、nonce、CAS 和原因。 */
  function leave() {
    Object.keys(lanes).forEach(abortLane);
    readIdentity = "";
    applyIdentity = "";
    navigation = "";
    refreshGeneration = 0;
    templates = [];
    selectedTemplateId = "";
    bindings = emptyBindings();
    preview = null;
    confirmation = null;
    result = null;
    pageError = null;
    pageGeneration += 1;
  }

  /** 判断状态机是否仍由当前读取身份和页面拥有。 */
  function ownedForRead() {
    return Boolean(readIdentity && navigation === "policy-templates");
  }

  /** apply owner 必须与当前 read owner 同时存在；降权后不能保留按钮或写 ticket。 */
  function ownedForApply() {
    return Boolean(ownedForRead() && applyIdentity);
  }

  /** 返回当前选中模板的内部安全副本。 */
  function selectedTemplate() {
    return templates.find((item) => item.policy_template_id === selectedTemplateId) ?? null;
  }

  /** 创建 lane ticket；ticket 同时绑定页面 generation 和 AbortController。 */
  function beginLane(name, facts) {
    abortLane(name);
    const controller = new AbortController();
    const ticket = Object.freeze({
      ...facts,
      pageGeneration,
      generation: lanes[name].generation + 1,
      signal: controller.signal,
    });
    lanes[name] = { generation: ticket.generation, controller, ticket, error: null };
    return ticket;
  }

  /** 判断异步结果仍属于当前页面、当前 lane 和未取消 ticket。 */
  function isCurrentLane(name, ticket) {
    return Boolean(
      ticket
      && lanes[name].ticket === ticket
      && ticket.pageGeneration === pageGeneration
      && !ticket.signal.aborted,
    );
  }

  /** 成功结束 lane，但保留递增 generation 供迟到结果比较。 */
  function finishLane(name, ticket) {
    if (!isCurrentLane(name, ticket)) return;
    lanes[name] = { generation: ticket.generation, controller: null, ticket: null, error: null };
  }

  /** 拒绝 lane 并公开有界错误，不保留原异常对象。 */
  function rejectLane(name, ticket, error) {
    if (!isCurrentLane(name, ticket)) return;
    lanes[name] = { generation: ticket.generation, controller: null, ticket: null, error };
    pageError = error;
  }

  /** 取消 lane；AbortError 不进入页面反馈。 */
  function abortLane(name) {
    const lane = lanes[name];
    lane.controller?.abort();
    lanes[name] = {
      generation: lane.generation + (lane.ticket ? 1 : 0),
      controller: null,
      ticket: null,
      error: null,
    };
  }

  return Object.freeze({
    syncOwner,
    beginCatalog,
    acceptCatalog,
    selectTemplate,
    setBinding,
    beginPreview,
    acceptPreview,
    openConfirmation,
    setReason,
    closeConfirmation,
    beginApply,
    acceptApply,
    reject,
    dismissFeedback,
    snapshot,
    leave,
  });
}

/** 创建空 lane，generation 只用于淘汰迟到结果。 */
function createLane() {
  return { generation: 0, controller: null, ticket: null, error: null };
}

/** 创建不会携带未知字段的空绑定。 */
function emptyBindings() {
  return { project_id: "", model_alias_id: "", threshold_cny: "" };
}

/** 按模板第一个后端 option 建立可操作默认值；真实适配仍以 preview 为准。 */
function defaultBindings(template) {
  const next = emptyBindings();
  if (template.required_bindings.includes("project_id")) {
    next.project_id = template.binding_options.projects[0]?.project_id ?? "";
  }
  // 只有模板显式要求模型别名时才建立默认值，避免把无关路由绑定带入预览请求。
  if (template.required_bindings.includes("model_alias_id")) {
    next.model_alias_id = template.binding_options.model_aliases[0]?.model_alias_id ?? "";
  }
  return next;
}

/** 复制单条后端模板，只接纳页面需要的固定字段。 */
function copyTemplate(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const compatibility = value.compatibility ?? {};
  const impact = value.impact ?? null;
  return {
    policy_template_id: String(value.policy_template_id ?? ""),
    template_code: String(value.template_code ?? ""),
    name: safeText(value.name, 80),
    description: safeText(value.description, 500),
    category: safeText(value.category, 32),
    scenario: safeText(value.scenario, 80),
    version: Number(value.version),
    priority: Number(value.priority),
    condition_blueprint: safeText(value.condition_blueprint, 1024),
    action_type: String(value.action_type ?? ""),
    required_bindings: Array.isArray(value.required_bindings)
      ? value.required_bindings.map(String)
      : [],
    binding_options: copyOptions(value.binding_options),
    compatibility: {
      status: String(compatibility.status ?? ""),
      reason_codes: safeStringList(compatibility.reason_codes, 32),
      warning_codes: safeStringList(compatibility.warning_codes, 32),
      can_apply: compatibility.can_apply === true,
      requires_permissions: safeStringList(compatibility.requires_permissions, 4),
    },
    impact: copyImpact(impact),
    preview_nonce: safeHash(value.preview_nonce),
    preview_as_of: safeTimestamp(value.preview_as_of),
    impact_hash: safeHash(value.impact_hash),
    before_candidate_hash: safeHash(value.before_candidate_hash),
    after_candidate_hash: safeHash(value.after_candidate_hash),
    expected_head_version: Number(value.expected_head_version),
  };
}

/** 复制 Project/Alias 最小目录，拒绝把 Provider 或模型内部字段带进状态。 */
function copyOptions(value) {
  const projects = Array.isArray(value?.projects) ? value.projects : [];
  const aliases = Array.isArray(value?.model_aliases) ? value.model_aliases : [];
  return {
    projects: projects.slice(0, 200).map((item) => ({
      project_id: String(item?.project_id ?? ""),
      name: safeText(item?.name, 120),
    })),
    model_aliases: aliases.slice(0, 200).map((item) => ({
      model_alias_id: String(item?.model_alias_id ?? ""),
      alias: safeText(item?.alias, 120),
      environment_code: safeText(item?.environment_code, 64),
    })),
  };
}

/** 复制精确影响摘要；排序只展示后端结论，浏览器不能提交覆盖。 */
function copyImpact(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const order = Array.isArray(value.evaluation_order) ? value.evaluation_order : [];
  return {
    project_id: String(value.project_id ?? ""),
    evaluation_order: order.slice(0, 201).map((item) => ({
      policy_id: String(item?.policy_id ?? ""),
      priority: Number(item?.priority),
      action_type: String(item?.action_type ?? ""),
      generated: item?.generated === true,
      shadowing: String(item?.shadowing ?? "none"),
    })),
    before_policy_count: Number(value.before_policy_count),
    after_policy_count: Number(value.after_policy_count),
    expected_head_version: Number(value.expected_head_version),
    rollback_target_snapshot_id: String(value.rollback_target_snapshot_id ?? ""),
    shadowing_policy_ids: safeStringList(value.shadowing_policy_ids, 200),
    derived_target_model_id: safeId(value.derived_target_model_id),
    derived_target_model_name: safeText(value.derived_target_model_name, 120),
    allowed_model_alias_names: safeStringList(value.allowed_model_alias_names, 20),
    frozen_environment_codes: safeStringList(value.frozen_environment_codes, 20),
  };
}

/** 验证目录投影的固定形态，compatibility 可为 unbound。 */
function validCatalogTemplate(item) {
  return TEMPLATE_ID_PATTERN.test(item.policy_template_id)
    && TEMPLATE_CODES.has(item.template_code)
    && item.name
    && item.description
    && Number.isSafeInteger(item.version)
    && item.version >= 1
    && Number.isSafeInteger(item.priority)
    && ["deny", "mask", "route_to"].includes(item.action_type)
    && ["unbound", "ready", "ready_with_warnings", "incompatible"].includes(
      item.compatibility.status,
    )
    && validRequiredBindings(item.required_bindings)
    && item.binding_options.projects.every(
      (option) => RESOURCE_ID_PATTERN.test(option.project_id) && option.name,
    )
    && item.binding_options.model_aliases.every(
      (option) => RESOURCE_ID_PATTERN.test(option.model_alias_id) && option.alias,
    );
}

/**
 * 验证精确影响的业务不变量。
 *
 * 后端 hash/CAS 负责防篡改，前端仍需失败关闭损坏响应，避免确认框把错误 Project、排序或
 * Snapshot 版本展示给审批人。bindings 是发起请求时冻结的 exact-set，不读取可伪造 DOM。
 */
function validReadyPreviewTemplate(item, bindings) {
  return validCatalogTemplate(item)
    && ["ready", "ready_with_warnings"].includes(item.compatibility.status)
    && item.compatibility.reason_codes.length === 0
    && (item.compatibility.status !== "ready" || item.compatibility.warning_codes.length === 0)
    && (item.compatibility.status !== "ready_with_warnings"
      || item.compatibility.warning_codes.length > 0)
    && item.preview_nonce
    && item.preview_as_of
    && item.impact_hash
    && item.before_candidate_hash
    && item.after_candidate_hash
    && Number.isSafeInteger(item.expected_head_version)
    && item.expected_head_version >= 1
    && validPreviewImpact(item, bindings, item.expected_head_version);
}

/**
 * 接纳后端合法的不可应用预览，但要求其不携带任何可被误作写凭证的 nonce、hash 或 CAS。
 * 这使页面能展示稳定阻断原因，同时不会让 incompatible 结果进入确认或 apply 路径。
 */
function validIncompatiblePreviewTemplate(item, bindings) {
  return validCatalogTemplate(item)
    && item.compatibility.status === "incompatible"
    && item.compatibility.can_apply === false
    && item.compatibility.reason_codes.length > 0
    && !item.preview_nonce
    && !item.preview_as_of
    && !item.impact_hash
    && !item.before_candidate_hash
    && !item.after_candidate_hash
    && item.expected_head_version === 0
    && item.binding_options.projects.some((option) => option.project_id === bindings.project_id)
    && (item.impact === null
      || validPreviewImpact(item, bindings, item.impact.expected_head_version));
}

/** 验证 ready/incompatible 共用的影响排序与 Project、计数、回滚基线不变量。 */
function validPreviewImpact(item, bindings, expectedHeadVersion) {
  const impact = item.impact;
  const order = impact?.evaluation_order ?? [];
  const generated = order.filter((entry) => entry.generated);
  const shadowingIds = order
    .filter((entry) => entry.shadowing !== "none")
    .map((entry) => entry.policy_id)
    .sort();
  const declaredShadowingIds = [...(impact?.shadowing_policy_ids ?? [])].sort();
  return impact
    && impact.project_id === bindings.project_id
    && item.binding_options.projects.some((option) => option.project_id === bindings.project_id)
    && Number.isSafeInteger(impact.before_policy_count)
    && impact.before_policy_count >= 0
    && impact.before_policy_count <= 200
    && Number.isSafeInteger(impact.after_policy_count)
    && impact.after_policy_count === impact.before_policy_count + 1
    && order.length === impact.after_policy_count
    && order.length <= 201
    && new Set(order.map((entry) => entry.policy_id)).size === order.length
    && order.every(validImpactOrderItem)
    && generated.length === 1
    && generated[0].priority === item.priority
    && generated[0].action_type === item.action_type
    && Number.isSafeInteger(expectedHeadVersion)
    && expectedHeadVersion >= 1
    && impact.expected_head_version === expectedHeadVersion
    && SNAPSHOT_ID_PATTERN.test(impact.rollback_target_snapshot_id)
    && declaredShadowingIds.every((policyId) => POLICY_ID_PATTERN.test(policyId))
    && new Set(declaredShadowingIds).size === declaredShadowingIds.length
    && JSON.stringify(declaredShadowingIds) === JSON.stringify(shadowingIds);
}

/** 验证影响排序单项；未知动作或遮蔽枚举不能进入审批摘要。 */
function validImpactOrderItem(item) {
  return POLICY_ID_PATTERN.test(item.policy_id)
    && Number.isSafeInteger(item.priority)
    && item.priority >= 1
    && item.priority <= 10000
    && ["allow", "deny", "mask", "route_to", "log_only"].includes(item.action_type)
    && ["none", "warning", "blocking"].includes(item.shadowing);
}

/** required_bindings 只允许三个冻结字段且 project_id 必选。 */
function validRequiredBindings(value) {
  return Array.isArray(value)
    && value.length >= 1
    && value.length <= 2
    && value[0] === "project_id"
    && new Set(value).size === value.length
    && value.every((item) => ["project_id", "model_alias_id", "threshold_cny"].includes(item));
}

/** 验证当前表单是否满足所选模板的绑定合同。 */
function validBindings(template, value) {
  return template.required_bindings.every((name) => {
    const current = String(value[name] ?? "").trim();
    if (name === "threshold_cny") {
      return /^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?$/.test(current);
    }
    return RESOURCE_ID_PATTERN.test(current);
  });
}

/** 只构造当前模板 exact-set 绑定，避免空的无关字段进入请求。 */
function requestBindings(template, value) {
  return Object.fromEntries(template.required_bindings.map((name) => [name, String(value[name])]));
}

/** 生成 DOM 可见模板副本，显式移除 nonce、hash 和候选 CAS。 */
function publicTemplate(item) {
  return {
    policyTemplateId: item.policy_template_id,
    templateCode: item.template_code,
    name: item.name,
    description: item.description,
    category: item.category,
    scenario: item.scenario,
    version: item.version,
    priority: item.priority,
    actionType: item.action_type,
    requiredBindings: [...item.required_bindings],
    bindingOptions: {
      projects: item.binding_options.projects.map((option) => ({ ...option })),
      modelAliases: item.binding_options.model_aliases.map((option) => ({ ...option })),
    },
    compatibility: {
      status: item.compatibility.status,
      reasonCodes: [...item.compatibility.reason_codes],
      warningCodes: [...item.compatibility.warning_codes],
      canApply: item.compatibility.can_apply,
      requiresPermissions: [...item.compatibility.requires_permissions],
    },
  };
}

/** 生成确认和影响区所需的安全预览，不公开 nonce、CAS 或完整 hash。 */
function publicPreview(value) {
  const item = value.template;
  return {
    template: publicTemplate(item),
    impact: item.impact
      ? {
        ...item.impact,
        evaluation_order: item.impact.evaluation_order.map((entry) => ({ ...entry })),
        shadowing_policy_ids: [...item.impact.shadowing_policy_ids],
        allowed_model_alias_names: [...item.impact.allowed_model_alias_names],
        frozen_environment_codes: [...item.impact.frozen_environment_codes],
      }
      : null,
    previewAsOf: item.preview_as_of,
  };
}

/** 把异常压缩为统一公开字段；details、stack、URL 和请求正文全部丢弃。 */
function publicError(error) {
  return Object.freeze({
    status: Number(error?.status) || 503,
    code: /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(String(error?.code ?? ""))
      ? String(error.code)
      : "policy_template.unavailable",
    message: safeText(error?.message, 240) || "策略模板请求失败",
    suggestion: safeText(error?.suggestion, 240),
    requestId: safeId(error?.requestId) || "-",
  });
}

/** 返回响应完整性错误，不包含原始响应或未知字段。 */
function integrityError() {
  return Object.freeze({
    status: 503,
    code: "policy_template.unavailable",
    message: "策略模板响应完整性校验失败",
    suggestion: "请刷新页面后重试",
    requestId: "-",
  });
}

/** 复制固定数量的公开原因码或权限码。 */
function safeStringList(value, maximum) {
  if (!Array.isArray(value) || value.length > maximum) return [];
  return value.map((item) => safeText(item, 128)).filter(Boolean);
}

/** 只接纳 64 位小写 hash；空值表示目录尚无精确预览。 */
function safeHash(value) {
  const normalized = String(value ?? "");
  return HASH_PATTERN.test(normalized) ? normalized : "";
}

/** 接纳后端 UTC 预览时间，不做本地时区推导。 */
function safeTimestamp(value) {
  const normalized = String(value ?? "");
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(normalized)
    ? normalized
    : "";
}

/** 接纳页面可显示的短文本，拒绝控制字符和 Unicode 代理字符。 */
function safeText(value, maximum) {
  const normalized = String(value ?? "");
  return normalized.length <= maximum && !containsControlCharacter(normalized) ? normalized : "";
}

/** 接纳公开资源或链路 ID，不允许路径和空白字符。 */
function safeId(value) {
  const normalized = String(value ?? "");
  return RESOURCE_ID_PATTERN.test(normalized) ? normalized : "";
}

/** 识别 C0/C1 控制字符与代理字符，保护 URL、DOM 和错误边界。 */
function containsControlCharacter(value) {
  return /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(String(value ?? ""));
}
