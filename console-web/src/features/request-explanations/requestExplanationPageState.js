/**
 * 模块职责：管理 PAGE-079 的身份 owner、一次性日志跳转、GET 代次和重绘 UI 状态。
 * 维护边界：所有状态仅驻留内存；tenant、用户、角色、Session 或 revision 任一变化都会
 * abort 并销毁旧数据，source_request_id 不得写入 URL、localStorage 或 sessionStorage。
 */

import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";

// 导航、请求 ID 和区块集合共同冻结页面 owner 与可交互边界，不能由响应动态扩展。
const REQUEST_EXPLANATION_NAVIGATION = "request-explanations";
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;
const REQUEST_EXPLANATION_SECTION_KEYS = new Set([
  "authentication",
  "rate_limit",
  "budget",
  "dlp",
  "policy",
  "routing",
  "keypool",
  "provider_response",
  "usage_and_finops",
  "logs",
  "webhooks",
]);
// 可恢复焦点只接受固定控件名称，避免任意 selector 或外部文本进入 DOM 查询。
const REQUEST_EXPLANATION_FOCUS_TARGETS = new Set([
  "request-id",
  "query",
  "refresh",
  "clear",
]);

/**
 * 从认证上下文构造完整身份快照。
 *
 * 返回值仅用于内存等值比较；缺少 tenant/user 或角色不允许读取时返回空串，使手写 DOM
 * 和未授权 hash 都无法拥有页面状态或触发网络请求。
 */
export function requestExplanationIdentitySnapshot(authContext) {
  // owner 绑定后端解释读取权限、矩阵 revision 与指纹，手写 hash 无法绕过降权。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.REQUEST_EXPLANATION_READ);
}

/** request_id 只允许后端冻结的 ASCII 标识格式，避免 path 分隔符或空白被浏览器规范化。 */
export function normalizeRequestExplanationId(value) {
  // source_request_id 是外部不透明标识；禁止 trim，否则两个不同输入会被静默折叠成同一请求。
  const candidate = text(value);
  return REQUEST_ID_PATTERN.test(candidate) ? candidate : null;
}

/**
 * 创建独立于 Request Logs 页面的单次 handoff。
 *
 * 日志页在离开时会立即销毁自身 selectedRequestId，因此必须先把 ID 写入这个全身份绑定
 * 仓库，再切换导航；目标页成功消费后立即删除，刷新和返回都不会重复携带旧请求。
 */
export function createRequestExplanationHandoff() {
  let entry = null;
  let generation = 0;

  return {
    /** 写入一次安全跳转；不完整 owner 或非法 ID 会先清空旧值并返回 false。 */
    stage({ identity, tenantId, requestId, source = "request_logs" }) {
      const normalizedIdentity = text(identity);
      const normalizedTenantId = text(tenantId).trim();
      const normalizedRequestId = normalizeRequestExplanationId(requestId);
      generation += 1;
      entry = normalizedIdentity && normalizedTenantId && normalizedRequestId
        ? Object.freeze({
            identity: normalizedIdentity,
            tenantId: normalizedTenantId,
            requestId: normalizedRequestId,
            source: source === "request_logs" ? "request_logs" : "manual",
            generation,
          })
        : null;
      return Boolean(entry);
    },

    /** 只有目标导航和完整身份完全相同才消费；不匹配值直接销毁，防止跨租户滞留。 */
    consume({ identity, tenantId, navigation }) {
      const current = entry;
      entry = null;
      if (
        !current
        || current.identity !== text(identity)
        || current.tenantId !== text(tenantId).trim()
        || text(navigation) !== REQUEST_EXPLANATION_NAVIGATION
      ) {
        return null;
      }
      return { requestId: current.requestId, source: current.source };
    },

    /** 登出、角色变化、导航失败或 pagehide 会同步清除尚未消费的 ID。 */
    clear() {
      generation += 1;
      entry = null;
    },

    /** 返回不含认证内容的测试快照。 */
    snapshot() {
      // pending 足以验证一次性语义；request_id 不应通过诊断快照形成第二条读取通道。
      return { generation, pending: Boolean(entry) };
    },
  };
}

/**
 * 创建请求解释页的读生命周期仓库。
 *
 * 显式查询或刷新才推进 desiredRevision；15 秒全页刷新仅复用已接纳快照，不重复 GET。
 * AbortController 是资源回收手段，identity/tenant/navigation/generation 才是迟到响应门禁。
 */
export function createRequestExplanationPageMemory() {
  let activeIdentity = "";
  let activeTenantId = "";
  let activeNavigation = "";
  let generation = 0;
  let desiredRevision = 0;
  let loadedRevision = 0;
  let sourceRequestId = null;
  let selectionSource = "manual";
  let controller = null;
  let activeTicket = null;
  let payload = null;
  let error = null;
  // 未提交输入、details 展开态和焦点目标驻留 owner 内存，避免 15 秒整页重绘破坏操作现场。
  let draftRequestId = "";
  let sectionDisclosure = {};
  let focusTarget = "";

  /** 结束活动 GET；controller 和 ticket 同时清空，迟到 finally 不能误清理新 owner。 */
  function abortRead() {
    controller?.abort();
    controller = null;
    activeTicket = null;
  }

  /** 销毁所有可能识别上一租户请求的状态。 */
  function invalidate({ resetOwner = false } = {}) {
    abortRead();
    generation += 1;
    desiredRevision = 0;
    loadedRevision = 0;
    sourceRequestId = null;
    selectionSource = "manual";
    payload = null;
    error = null;
    draftRequestId = "";
    sectionDisclosure = {};
    focusTarget = "";
    if (resetOwner) {
      activeIdentity = "";
      activeTenantId = "";
      activeNavigation = "";
    }
  }

  /** 进入 PAGE-079 owner；身份或导航变化必须在采用新 owner 前清空旧数据。 */
  function enter({ identity, tenantId, navigation }) {
    const nextIdentity = text(identity);
    const nextTenantId = text(tenantId).trim();
    const nextNavigation = text(navigation);
    if (
      !nextIdentity
      || !nextTenantId
      || nextNavigation !== REQUEST_EXPLANATION_NAVIGATION
    ) {
      invalidate({ resetOwner: true });
      return false;
    }
    if (
      nextIdentity !== activeIdentity
      || nextTenantId !== activeTenantId
      || nextNavigation !== activeNavigation
    ) {
      invalidate({ resetOwner: true });
      activeIdentity = nextIdentity;
      activeTenantId = nextTenantId;
      activeNavigation = nextNavigation;
    }
    return true;
  }

  /** 对比读取票据与主应用实时 owner，refreshId 变化后旧 401 也不能结束新 Session。 */
  function isCurrent(ticket, owner = {}) {
    return Boolean(
      ticket
      && ticket === activeTicket
      && !ticket.signal.aborted
      && ticket.generation === generation
      && ticket.identity === activeIdentity
      && ticket.tenantId === activeTenantId
      && ticket.navigation === activeNavigation
      && ticket.identity === text(owner.identity)
      && ticket.tenantId === text(owner.tenantId).trim()
      && ticket.navigation === text(owner.navigation)
      && ticket.requestId === sourceRequestId
      && ticket.readRevision === desiredRevision
      && ticket.refreshId === owner.refreshId
      && ticket.refreshId === owner.currentRefreshId,
    );
  }

  return {
    /** 选择或重新加载一条请求；即使 ID 相同，显式动作也创建新的读取代次。 */
    select({ identity, tenantId, navigation, requestId, source = "manual" }) {
      if (!enter({ identity, tenantId, navigation })) {
        return false;
      }
      const normalizedRequestId = normalizeRequestExplanationId(requestId);
      abortRead();
      generation += 1;
      desiredRevision += 1;
      loadedRevision = 0;
      sourceRequestId = normalizedRequestId;
      draftRequestId = text(requestId);
      selectionSource = source === "request_logs" ? "request_logs" : "manual";
      payload = null;
      error = normalizedRequestId
        ? null
        : safeError({
            code: "request_explanation.request_id_invalid",
            message: "request_id 格式不合法",
            requestId: "-",
          });
      // 新查询对应一份新历史快照，沿用上一请求的折叠偏好可能掩盖新异常。
      sectionDisclosure = {};
      return Boolean(normalizedRequestId);
    },

    /**
     * 保存尚未提交的 request_id 草稿。
     * 浏览器 maxlength 已限制 128；状态机再次拒绝超长值，但绝不 trim、截断或折叠外部 ID。
     */
    setDraft({ identity, tenantId, navigation, requestId }) {
      if (!enter({ identity, tenantId, navigation })) {
        return false;
      }
      const candidate = text(requestId);
      if (candidate.length > 128) {
        return false;
      }
      draftRequestId = candidate;
      return true;
    },

    /** 保存固定证据区块的展开状态；未知 key 失败关闭，不能借动态字段扩展 DOM。 */
    setSectionDisclosure({ identity, tenantId, navigation, sectionKey, open }) {
      const normalizedSectionKey = text(sectionKey);
      if (
        !enter({ identity, tenantId, navigation })
        || !REQUEST_EXPLANATION_SECTION_KEYS.has(normalizedSectionKey)
      ) {
        return false;
      }
      sectionDisclosure = {
        ...sectionDisclosure,
        [normalizedSectionKey]: open === true,
      };
      return true;
    },

    /**
     * 保存可恢复焦点的固定控件名。
     * section 前缀只接受冻结区块，禁止把任意 selector 或动态文本写入页面内存。
     */
    setFocusTarget({ identity, tenantId, navigation, target }) {
      if (!enter({ identity, tenantId, navigation })) {
        return false;
      }
      const normalizedTarget = normalizeFocusTarget(target);
      if (!normalizedTarget && text(target)) {
        return false;
      }
      focusTarget = normalizedTarget;
      return true;
    },

    /** 清空当前查询但保留已确认页面 owner，使页面回到 idle，而不是制造非法 ID 错误。 */
    clearSelection({ identity, tenantId, navigation }) {
      if (!enter({ identity, tenantId, navigation })) {
        return false;
      }
      abortRead();
      generation += 1;
      desiredRevision = 0;
      loadedRevision = 0;
      sourceRequestId = null;
      selectionSource = "manual";
      payload = null;
      error = null;
      draftRequestId = "";
      sectionDisclosure = {};
      focusTarget = "request-id";
      return true;
    },

    /** 只在尚未加载当前显式 revision 时启动 GET；定时全页刷新不会产生第二个请求。 */
    beginRead({ identity, tenantId, navigation, refreshId, currentRefreshId }) {
      if (!enter({ identity, tenantId, navigation }) || !sourceRequestId) {
        return null;
      }
      // 当前 revision 已有成功或错误终态时复用内存快照，定时刷新不得重复产生访问事实。
      if (loadedRevision === desiredRevision && (payload || error)) {
        return null;
      }
      if (
      // 当前 revision 已有成功或错误终态时复用内存快照，定时刷新不得重复产生访问事实。
        activeTicket
        && activeTicket.refreshId === refreshId
        && !activeTicket.signal.aborted
      ) {
        return null;
      }
      abortRead();
      controller = new AbortController();
      error = null;
      const ticket = Object.freeze({
        identity: activeIdentity,
        tenantId: activeTenantId,
        navigation: activeNavigation,
        requestId: sourceRequestId,
        readRevision: desiredRevision,
        refreshId,
        currentRefreshId,
        generation: ++generation,
        source: selectionSource,
        controller,
        signal: controller.signal,
      });
      activeTicket = ticket;
      return ticket;
    },

    isCurrent,

    /** 响应必须回显相同 source_request_id；不匹配对象按串租户/错 owner 数据拒绝。 */
    acceptSuccess(ticket, result, owner) {
      if (!isCurrent(ticket, owner) || !payloadMatchesTicket(result, ticket)) {
        return false;
      }
      payload = cloneJson(result);
      loadedRevision = ticket.readRevision;
      error = null;
      return true;
    },

    /** 当前 owner 的异常只保留统一公开字段，不保留响应正文、Header 或堆栈。 */
    acceptFailure(ticket, failure, owner) {
      if (!isCurrent(ticket, owner)) {
        return false;
      }
      payload = null;
      loadedRevision = ticket.readRevision;
      error = safeError(failure);
      return true;
    },

    /** 返回当前选择；事件处理器不能直接修改仓库内部值。 */
    query() {
      return {
        requestId: sourceRequestId,
        draftRequestId,
        source: selectionSource,
        desiredRevision,
        loadedRevision,
        sectionDisclosure: { ...sectionDisclosure },
        focusTarget,
      };
    },

    /** 组装 renderer 状态；idle/loading/error/ok 均不暴露 owner 或认证材料。 */
    snapshot() {
      if (!sourceRequestId) {
        return error
          ? uiSnapshot({ state: "error", requestId: "", error })
          : uiSnapshot({ state: "idle", requestId: "" });
      }
      // 只有与 desiredRevision 完全一致的错误才可见，迟到失败不能覆盖更新后的查询。
      if (error && loadedRevision === desiredRevision) {
        return uiSnapshot({
          state: "error",
          requestId: sourceRequestId,
          error,
          readRevision: loadedRevision,
        });
      }
      if (payload && loadedRevision === desiredRevision) {
        return uiSnapshot({
          ...cloneJson(payload),
          state: "ok",
          requestId: sourceRequestId,
          source: selectionSource,
          readRevision: loadedRevision,
        });
      }
      return uiSnapshot({
        state: "loading",
        requestId: sourceRequestId,
      // 只有与 desiredRevision 完全一致的错误才可见，迟到失败不能覆盖更新后的查询。
        readRevision: desiredRevision,
      });
    },

    /** 登出、离页、身份变化和 pagehide 共用同一清理入口。 */
    leave() {
      invalidate({ resetOwner: true });
    },
  };

  /** 给每种 renderer 状态附加同一份低风险 UI 状态，供全页重绘后恢复。 */
  function uiSnapshot(state) {
    return {
      ...state,
      draftRequestId,
      sectionDisclosure: { ...sectionDisclosure },
      focusTarget,
    };
  }
}

/** 焦点目标只能是固定控件或冻结区块的 summary，绝不能接收任意 CSS selector。 */
function normalizeFocusTarget(value) {
  const candidate = text(value);
  if (REQUEST_EXPLANATION_FOCUS_TARGETS.has(candidate)) {
    return candidate;
  }
  if (candidate.startsWith("section:")) {
    const sectionKey = candidate.slice("section:".length);
    return REQUEST_EXPLANATION_SECTION_KEYS.has(sectionKey) ? candidate : "";
  }
  return "";
}

/** GET 结果必须是单个 RequestExplanation，且源 ID 与 path ticket 完全一致。 */
function payloadMatchesTicket(result, ticket) {
  const explanation = result?.request_explanation;
  return Boolean(
    explanation
    && typeof explanation === "object"
    && !Array.isArray(explanation)
    && normalizeRequestExplanationId(explanation.source_request_id) === ticket.requestId
    && text(result?.request_id).trim()
    && text(result?.trace_id).trim(),
  );
}

/** 错误内存仅保留公开字段，避免 Error 对象、response 或 stack 被 renderer 隐式序列化。 */
function safeError(error, fallbackMessage = "请求解释读取失败") {
  return {
    code: text(error?.code),
    message: text(error?.message || fallbackMessage),
    requestId: text(error?.requestId ?? error?.request_id ?? "-") || "-",
  };
}

/** JSON 响应深复制后再保存，调用方后续修改 fixture/对象不会改变已接纳的租户快照。 */
function cloneJson(value) {
  return value === null || value === undefined
    ? value
    : JSON.parse(JSON.stringify(value));
}

/** 统一把可空 owner 和字段转为文本，禁止对象在身份比较中隐式泄露。 */
function text(value) {
  return value === null || value === undefined ? "" : String(value);
}
