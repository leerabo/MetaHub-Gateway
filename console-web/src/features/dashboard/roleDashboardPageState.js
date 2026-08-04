/**
 * PAGE-092 角色首页的授权 owner、响应投影和访问事件生命周期。
 *
 * 本模块只保存当前页面内存，不写 URL、LocalStorage、SessionStorage 或用户偏好。Token 仅存在于
 * 私有 ticket 中用于等值判断，公开 snapshot 永不返回 Token、tenant、Session 或授权指纹。
 */

import {
  authorizationIdentitySnapshot,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 五个固定视图和六张固定卡片来自服务端产品目录；前端只做合同收窄，不能用它们替代后端 RBAC。
const VIEW_CODES = Object.freeze([
  "executive",
  "finance",
  "security",
  "developer",
  "operations",
]);
// Set 只用于 exact-match 校验，不赋予任何视图权限；真实可见集合始终来自后端响应。
const VIEW_CODE_SET = new Set(VIEW_CODES);
// 六卡目录限制响应投影的字段面，未知卡片不会因前端扩展逻辑进入受保护 DOM。
const CARD_CODE_SET = new Set([
  "cost",
  "security",
  "stability",
  "adoption",
  "provider_health",
  "budget_risk",
]);
// 状态和单位目录只约束展示合同，不在浏览器重新计算治理阈值或成本。
const STATUS_SET = new Set(["ok", "warning", "critical", "empty"]);
const UNIT_SET = new Set(["count", "percent", "ms", "cny", "tokens", "steps"]);
// 事件来源必须与后端 exact-set 一致，防止调用方把自由文本借遥测写入审计表。
const EVENT_SOURCE_SET = new Set(["sidebar", "direct_hash", "view_switch", "refresh"]);
// 链路/导航 ID、事实 code 和配置 ID 分别使用独立格式，避免宽松规则扩大 DOM 字段面。
const SAFE_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,256}$/;
const SAFE_FACT_CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const CONFIG_ID_PATTERN = /^rdc_[0-9a-f]{32}$/;

/**
 * 生成角色首页完整授权身份摘要。
 *
 * 共享 helper 已冻结 tenant、user、role、Session、permission revision 和 authorization fingerprint；
 * 任一字段缺失或 dashboard.read 被撤销都会返回空串，角色名本身不能补回权限。
 */
export function roleDashboardIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.DASHBOARD_READ);
}

/**
 * 判断当前 shell 是否仍由后端授权的 overview 导航拥有。
 *
 * hashState 由主应用实时解析；非法 hash 或明确指向其他页面时立即失败，不能等待下一轮导航响应。
 */
export function roleDashboardNavigationOwned(consoleShellState, hashState = {}) {
  if (consoleShellState?.activeNavigationItemId !== "overview") return false;
  if (hashState?.rejected || (hashState?.value && hashState.value !== "overview")) return false;
  const items = consoleShellState?.navigation?.navigation_items;
  return Array.isArray(items) && items.some((item) => (
    item?.id === "overview"
    && item?.target_section === "overview"
    && item?.visible !== false
    && item?.disabled !== true
  ));
}

/** 返回固定视图目录副本，调用方不能修改模块级产品上限。 */
export function roleDashboardViewCodes() {
  return [...VIEW_CODES];
}

/**
 * 把 PAGE-092 成功响应投影为可进入 DOM 的最小白名单对象。
 *
 * 返回 null 表示响应违反固定数量、枚举、owner ID、数值或视图一致性合同。所有未知字段都会被丢弃，
 * 因此 Provider Key、Token、Prompt、Response、Endpoint 或任意测试 canary 不会随扩展字段进入页面状态。
 */
export function sanitizeRoleDashboardPayload(payload) {
  const config = sanitizeConfig(payload?.config);
  const windowInfo = sanitizeWindow(payload?.window);
  const cards = sanitizeCards(payload?.cards);
  const emptyState = sanitizeEmptyState(payload?.empty_state);
  const requestId = safeId(payload?.request_id);
  const traceId = safeId(payload?.trace_id);
  if (!config || !windowInfo || !cards || !emptyState || !requestId || !traceId) return null;
  return Object.freeze({
    config,
    window: windowInfo,
    cards,
    empty_state: emptyState,
    request_id: requestId,
    trace_id: traceId,
  });
}

/**
 * 创建角色首页状态机。
 *
 * Abort 负责尽快终止旧网络请求；generation、完整 authorization identity、导航、视图和 Token 的
 * 等值判断负责淘汰浏览器或测试 stub 无法真正取消的迟到 200/401/403/503。
 */
export function createRoleDashboardPageState() {
  let generation = 0;
  let viewGeneration = 0;
  let operation = null;
  let protectedPayload = null;
  let protectedOwner = null;
  let publicState = null;
  let eventAttemptKey = "";
  let eventOperation = null;
  let eventWarning = null;
  let acceptedSource = "refresh";
  let acceptedSourceViewGeneration = 0;

  /** 当前 owner 必须逐项匹配；AbortSignal 不能单独证明结果仍属于当前页面。 */
  function owns(ticket, current) {
    return Boolean(
      ticket
      && operation === ticket
      && ticket.generation === generation
      && !ticket.controller.signal.aborted
      && ticket.identity === current?.identity
      && ticket.navigation === current?.navigation
      && ticket.requestedView === normalizeView(current?.requestedView)
      && ticket.token === current?.token,
    );
  }

  /** 清除受保护响应，但保留可追踪的安全错误对象供当前 owner 渲染。 */
  function replaceWithError(error) {
    protectedPayload = null;
    protectedOwner = null;
    const safeError = publicDashboardError(error);
    publicState = Object.freeze({ state: "error", ...safeError });
    return publicState;
  }

  return {
    /**
     * 开始一次默认、自动刷新或显式切换读取。
     *
     * transition=true 表示用户切换视图：旧请求和旧卡片会在 fetch 前同步销毁，并推进视图代次。
     * 自动刷新保留同 owner、同 selected view 的已净化快照，只有 503 才可能降级为 partial。
     */
    begin({
      identity,
      navigation,
      requestedView = "",
      availableViewCodes = [],
      token,
      source = "refresh",
      transition = false,
    }) {
      const normalizedView = normalizeView(requestedView);
      const allowedViews = normalizeAvailableViews(availableViewCodes);
      if (
        !identity
        || navigation !== "overview"
        || !token
        || (requestedView && !normalizedView)
        || (normalizedView && !allowedViews.includes(normalizedView))
      ) {
        return null;
      }
      operation?.controller.abort();
      if (transition) {
        // 切换标签后绝不能出现“新标签 + 旧卡片”；先销毁受保护 DOM 数据，再开始网络请求。
        protectedPayload = null;
        protectedOwner = null;
        publicState = Object.freeze({ state: "loading", selectedView: normalizedView });
        eventWarning = null;
        // 遥测请求不可取消，但移除 owner 后其迟到 401/失败将被 ownsEvent 丢弃，新视图可立即认领自己的事件。
        eventOperation = null;
        viewGeneration += 1;
      } else if (viewGeneration === 0) {
        viewGeneration = 1;
      }
      const canonicalSource = EVENT_SOURCE_SET.has(source) ? source : "refresh";
      operation = Object.freeze({
        generation: ++generation,
        viewGeneration,
        identity,
        navigation,
        requestedView: normalizedView,
        availableViewCodes: Object.freeze(allowedViews),
        source: canonicalSource,
        token,
        controller: new AbortController(),
      });
      return operation;
    },

    owns,

    /**
     * 接纳当前 ticket 的成功响应，并丢弃所有未审计扩展字段。
     * 显式请求必须返回同一 selected view；后端目录若不包含 selected view 也按畸形响应失败关闭。
     */
    accept(ticket, payload, current) {
      if (!owns(ticket, current)) return null;
      const sanitized = sanitizeRoleDashboardPayload(payload);
      if (
        !sanitized
        || (ticket.requestedView && sanitized.config.selected_view_code !== ticket.requestedView)
      ) {
        operation = null;
        viewGeneration += 1;
        return replaceWithError({
          status: 500,
          code: "dashboard.response_invalid",
          message: "角色首页响应不完整",
          requestId: safeId(payload?.request_id) || "-",
        });
      }
      protectedPayload = sanitized;
      // 同一视图代次在 DOM 投影前即使又完成自动刷新，也保留首次进入来源，不能改写成 refresh。
      if (acceptedSourceViewGeneration !== ticket.viewGeneration) {
        acceptedSource = ticket.source;
        acceptedSourceViewGeneration = ticket.viewGeneration;
      }
      protectedOwner = Object.freeze({
        identity: ticket.identity,
        navigation: ticket.navigation,
        selectedView: sanitized.config.selected_view_code,
        viewGeneration: ticket.viewGeneration,
        // Token 只保存在私有 owner 中，阻止同 Session 令牌轮换后的旧响应生成新遥测；snapshot 永不暴露。
        token: ticket.token,
      });
      publicState = Object.freeze({
        state: "ok",
        ...sanitized,
        ...(eventWarning ? { eventWarning } : {}),
      });
      operation = null;
      return publicState;
    },

    /**
     * 接纳当前 ticket 的统一错误。
     * 只有 503、同完整 authorization owner 且同 selected view 才保留净化快照为 partial；401、403、
     * 配置错误和跨视图失败都会清空配置、分段、卡片与命令，避免旧受保护 DOM 残留。
     */
    reject(ticket, error, current) {
      if (!owns(ticket, current)) return null;
      operation = null;
      const safeError = publicDashboardError(error);
      const canRetainPartial = error?.status === 503
        && protectedPayload
        && protectedOwner?.identity === ticket.identity
        && protectedOwner?.navigation === ticket.navigation
        && protectedOwner?.selectedView === ticket.requestedView
        && protectedOwner?.viewGeneration === ticket.viewGeneration;
      if (canRetainPartial) {
        publicState = Object.freeze({
          state: "partial",
          ...protectedPayload,
          error: Object.freeze(safeError),
          ...(eventWarning ? { eventWarning } : {}),
        });
        return publicState;
      }
      viewGeneration += 1;
      return replaceWithError(error);
    },

    /** 只释放仍为当前 operation 的 ticket；旧 finally 不能清除之后建立的新请求。 */
    finish(ticket) {
      if (operation === ticket && ticket.generation === generation) operation = null;
    },

    /**
     * 返回当前可渲染快照。
     * 快照只含白名单业务摘要、公开错误和代次状态，不含 tenant、Session、权限指纹、Token 或 controller。
     */
    snapshot() {
      if (publicState) return publicState;
      return Object.freeze({ state: operation ? "loading" : "idle" });
    },

    /** 返回最近成功视图，仅用于同页自动刷新或构造受控 view query。 */
    selectedView() {
      return protectedPayload?.config?.selected_view_code ?? "";
    },

    /** 返回最近成功响应下发的可切换视图代码，调用方不得按角色名扩充。 */
    availableViewCodes() {
      return protectedPayload?.config?.available_views?.map((item) => item.code) ?? [];
    },

    /**
     * 在成功响应已经投影到当前 DOM 后认领一次访问事件。
     * 同一 Session owner、同一 viewGeneration、同一 selected view 只认领一次；认领即视为 attempted，
     * 即使遥测失败也不会由 15 秒刷新反复重试。
     */
    claimRenderedEvent({ identity, navigation, token, selectedView, configRevision }) {
      if (
        !protectedPayload
        || !protectedOwner
        || identity !== protectedOwner.identity
        || navigation !== protectedOwner.navigation
        || token !== protectedOwner.token
        || selectedView !== protectedOwner.selectedView
        || configRevision !== protectedPayload.config.config_revision
        || !token
      ) {
        return null;
      }
      const key = `${protectedOwner.viewGeneration}:${selectedView}`;
      if (eventAttemptKey === key || eventOperation) return null;
      eventAttemptKey = key;
      eventOperation = Object.freeze({
        generation,
        identity,
        navigation,
        viewGeneration: protectedOwner.viewGeneration,
        selectedView,
        configRevision,
        source: operation?.source ?? publicState?.source ?? "refresh",
        token,
      });
      // accept 会释放网络 operation，因此事件来源必须从最近接纳的响应单独恢复。
      if (EVENT_SOURCE_SET.has(acceptedSource)) {
        eventOperation = Object.freeze({ ...eventOperation, source: acceptedSource });
      }
      return eventOperation;
    },

    /** 当前事件的完整 owner 仍一致时才允许处理成功或错误，迟到 401 不能结束新 Session。 */
    ownsEvent(ticket, current) {
      return Boolean(
        ticket
        && eventOperation === ticket
        && ticket.identity === current?.identity
        && ticket.navigation === current?.navigation
        && ticket.token === current?.token
        && protectedOwner?.token === ticket.token
        && protectedOwner?.viewGeneration === ticket.viewGeneration
        && protectedOwner?.selectedView === ticket.selectedView
        && protectedPayload?.config?.config_revision === ticket.configRevision,
      );
    },

    /** 遥测完成后只释放当前事件引用；attempt key 保留以阻止自动刷新重放。 */
    finishEvent(ticket) {
      if (eventOperation === ticket) eventOperation = null;
    },

    /** 保存遥测统一错误的安全投影，页面可继续阅读已成功加载的角色首页。 */
    setEventWarning(error) {
      eventWarning = Object.freeze(publicDashboardError(error));
      if (publicState && ["ok", "partial"].includes(publicState.state)) {
        publicState = Object.freeze({ ...publicState, eventWarning });
      }
      return eventWarning;
    },

    /**
     * 登出、换租户、授权漂移、离开 overview 或 pagehide 时同步终止全部 owner 并销毁受保护数据。
     */
    clear() {
      generation += 1;
      viewGeneration += 1;
      operation?.controller.abort();
      operation = null;
      protectedPayload = null;
      protectedOwner = null;
      publicState = null;
      eventAttemptKey = "";
      eventOperation = null;
      eventWarning = null;
      acceptedSource = "refresh";
      acceptedSourceViewGeneration = 0;
    },

    /** 诊断快照只暴露非敏感生命周期计数，供单测确认清理和 Abort 行为。 */
    lifecycleSnapshot() {
      return Object.freeze({
        generation,
        viewGeneration,
        pending: Boolean(operation),
        eventPending: Boolean(eventOperation),
        hasProtectedPayload: Boolean(protectedPayload),
      });
    },
  };
}

/** 校验并冻结配置与服务端下发的可见视图目录。 */
function sanitizeConfig(config) {
  if (
    !config
    || typeof config.role_dashboard_config_id !== "string"
    || !CONFIG_ID_PATTERN.test(config.role_dashboard_config_id)
  ) return null;
  const revision = config.config_revision;
  const defaultView = normalizeView(config.default_view_code);
  const selectedView = normalizeView(config.selected_view_code);
  if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 1 || !defaultView || !selectedView) return null;
  if (!Array.isArray(config.available_views) || config.available_views.length < 1 || config.available_views.length > 5) return null;
  const available = [];
  const seen = new Set();
  for (const item of config.available_views) {
    const code = normalizeView(item?.code);
    const label = boundedText(item?.label, 64);
    if (
      !code
      || !label
      || seen.has(code)
      || typeof item?.is_default !== "boolean"
      || item.is_default !== (code === defaultView)
    ) return null;
    seen.add(code);
    available.push(Object.freeze({ code, label, is_default: item.is_default }));
  }
  // 实时撤权后 default 可以从 available 中消失；selected 必须是后端安全回落后的可见视图。
  if (!seen.has(selectedView)) return null;
  return Object.freeze({
    role_dashboard_config_id: config.role_dashboard_config_id,
    config_revision: revision,
    default_view_code: defaultView,
    selected_view_code: selectedView,
    available_views: Object.freeze(available),
  });
}

/** 校验固定业务日窗口；时间字符串只作为文本展示，不在前端改变后端统计边界。 */
function sanitizeWindow(windowInfo) {
  if (!windowInfo) return null;
  const timezone = boundedText(windowInfo.timezone, 64);
  const startAt = boundedText(windowInfo.start_at, 64);
  const endAt = boundedText(windowInfo.end_at, 64);
  const generatedAt = boundedText(windowInfo.generated_at, 64);
  if (!timezone || !startAt || !endAt || !generatedAt) return null;
  return Object.freeze({
    timezone,
    start_at: startAt,
    end_at: endAt,
    generated_at: generatedAt,
  });
}

/** 校验六张卡片的唯一性、数量、值域和事实上限，并保持后端原始顺序。 */
function sanitizeCards(cards) {
  if (!Array.isArray(cards) || cards.length !== 6) return null;
  const sanitized = [];
  const seen = new Set();
  for (const card of cards) {
    const code = typeof card?.code === "string" ? card.code : "";
    const status = typeof card?.status === "string" ? card.status : "";
    const unit = typeof card?.primary_unit === "string" ? card.primary_unit : "";
    const value = finiteNonNegative(card?.primary_value);
    const label = boundedText(card?.label, 64);
    const summary = boundedText(card?.summary, 160);
    const facts = sanitizeFacts(card?.facts);
    const target = card?.target_navigation_item_id == null
      ? null
      : safeId(card.target_navigation_item_id);
    if (
      !CARD_CODE_SET.has(code)
      || seen.has(code)
      || !STATUS_SET.has(status)
      || !UNIT_SET.has(unit)
      || value == null
      || !label
      || !summary
      || !facts
      || (card?.target_navigation_item_id != null && !target)
    ) return null;
    seen.add(code);
    sanitized.push(Object.freeze({
      code,
      label,
      status,
      primary_value: value,
      primary_unit: unit,
      summary,
      facts,
      target_navigation_item_id: target,
    }));
  }
  return Object.freeze(sanitized);
}

/** 校验卡片中最多五条低风险聚合事实；未知扩展字段和正文类字段不会进入投影。 */
function sanitizeFacts(facts) {
  if (!Array.isArray(facts) || facts.length > 5) return null;
  const result = [];
  const seen = new Set();
  for (const fact of facts) {
    const code = typeof fact?.code === "string" ? fact.code : "";
    const label = boundedText(fact?.label, 64);
    const unit = typeof fact?.unit === "string" ? fact.unit : "";
    const value = finiteNonNegative(fact?.value);
    if (!SAFE_FACT_CODE_PATTERN.test(code) || seen.has(code) || !label || !UNIT_SET.has(unit) || value == null) return null;
    seen.add(code);
    result.push(Object.freeze({ code, label, value, unit }));
  }
  return Object.freeze(result);
}

/** 校验空状态，只允许三项固定展示字段进入 DOM。 */
function sanitizeEmptyState(emptyState) {
  if (!emptyState || typeof emptyState.is_empty !== "boolean") return null;
  const title = boundedText(emptyState.title, 120, true);
  const description = boundedText(emptyState.description, 240, true);
  if (title == null || description == null) return null;
  // 空态为真时必须存在可见说明；非空态必须精确保持空文案，避免畸形成功响应制造自相矛盾的页面状态。
  if (
    (emptyState.is_empty && (!title.trim() || !description.trim()))
    || (!emptyState.is_empty && (title !== "" || description !== ""))
  ) return null;
  return Object.freeze({ is_empty: emptyState.is_empty, title, description });
}

/** 规范后端下发的视图集合，去重并保持原顺序。 */
function normalizeAvailableViews(values) {
  if (!Array.isArray(values)) return [];
  const result = [];
  for (const value of values) {
    const code = normalizeView(value);
    if (code && !result.includes(code)) result.push(code);
  }
  return result;
}

/** 只接受五个精确小写视图代码，不接受大小写变体或别名。 */
function normalizeView(value) {
  const code = typeof value === "string" ? value : "";
  return VIEW_CODE_SET.has(code) ? code : "";
}

/** 把统一错误收窄为可展示字段，原始响应正文、Header 和异常对象不会进入状态。 */
function publicDashboardError(error) {
  const errorCode = typeof error?.code === "string" ? error.code : "";
  const code = /^[a-z][a-z0-9_.]{1,127}$/.test(errorCode)
    ? errorCode
    : "dashboard.unavailable";
  return {
    status: typeof error?.status === "number" && Number.isSafeInteger(error.status)
      ? error.status
      : 0,
    code,
    message: boundedText(error?.message, 240) || "角色首页暂不可用",
    requestId: safeId(error?.requestId ?? error?.request_id) || "-",
    traceId: safeId(error?.traceId ?? error?.trace_id) || "-",
  };
}

/** 有界文本避免异常响应制造超大 DOM；allowEmpty 仅用于合法空状态。 */
function boundedText(value, maxLength, allowEmpty = false) {
  // 即使调用方允许空串，字段仍必须由后端显式返回字符串；缺失和 null 不能被静默补成合法合同。
  if (typeof value !== "string") return null;
  const text = value;
  if (text.length > maxLength || (!allowEmpty && !text)) return allowEmpty && !text ? "" : null;
  return text;
}

/** 公开链路和导航 ID 只能使用稳定安全字符。 */
function safeId(value) {
  const text = typeof value === "string" ? value : "";
  return SAFE_ID_PATTERN.test(text) ? text : "";
}

/** 指标只接受 JavaScript 可安全展示的有限非负数，NaN/Infinity 不得进入 DOM。 */
function finiteNonNegative(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}
