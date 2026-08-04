/**
 * 模块职责：维护 PAGE-070 调用反馈表单、PAGE-071 反馈报表及其彼此独立的异步 owner。
 * 安全边界：页面只消费后端白名单 DTO；权限、租户隔离和源调用可反馈资格仍由 Control API 强制执行。
 */

import { createRequestFeedbackRequestId, projectFeedbackInfo } from "../../lib/requestFeedback.js";
import {
  authorizationIdentitySnapshot,
  hasPermission,
  PERMISSIONS,
} from "../../lib/permissions.js";

/**
 * 固定标签字典同时决定表单顺序、客户端规范化顺序和中文兜底文案。
 * 标签仍会由后端再次校验，前端字典只用于减少误操作，不能替代安全边界。
 */
export const REQUEST_FEEDBACK_TAGS = Object.freeze([
  Object.freeze({ code: "accurate", label: "准确" }),
  Object.freeze({ code: "relevant", label: "相关" }),
  Object.freeze({ code: "complete", label: "完整" }),
  Object.freeze({ code: "helpful", label: "有帮助" }),
  Object.freeze({ code: "clear", label: "清晰" }),
  Object.freeze({ code: "inaccurate", label: "不准确" }),
  Object.freeze({ code: "irrelevant", label: "不相关" }),
  Object.freeze({ code: "incomplete", label: "不完整" }),
  Object.freeze({ code: "unsafe", label: "不安全" }),
  Object.freeze({ code: "slow", label: "响应慢" }),
]);

/** 标签 code 集合用于拒绝测试或扩展代码注入的自由文本标签。 */
const REQUEST_FEEDBACK_TAG_CODES = new Set(REQUEST_FEEDBACK_TAGS.map((item) => item.code));

/** 三组正反标签必须互斥；后端仍会执行同一业务约束。 */
const REQUEST_FEEDBACK_EXCLUSIVE_TAGS = Object.freeze([
  Object.freeze(["accurate", "inaccurate"]),
  Object.freeze(["relevant", "irrelevant"]),
  Object.freeze(["complete", "incomplete"]),
]);

/** PAGE-070 最多允许五个固定标签，与 Feedback API 合同一致。 */
const MAX_FEEDBACK_TAGS = 5;

/**
 * 从认证上下文生成反馈页面身份快照。
 * tenant、用户、角色、Session ID 或 revision 任一变化都会淘汰旧草稿、操作 ID 和迟到响应。
 */
export function requestFeedbackIdentitySnapshot(authContext) {
  // 反馈 owner 绑定后端 feedback.use、revision 与指纹，撤权后旧草稿和迟到回调全部失效。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.FEEDBACK_USE);
}

/**
 * 从 RequestLog 详情兼容读取反馈合同字段。
 * 并行后端可能暂时把字段放在详情根对象或 request_log 内；这里只读取三项资格字段和公开 Feedback DTO。
 */
export function requestFeedbackContractFromDetail(detail) {
  const envelope = detail?.request_log && typeof detail.request_log === "object"
    ? detail.request_log
    : {};
  const log = envelope.log && typeof envelope.log === "object" ? envelope.log : {};
  const feedback = projectFeedbackInfo(
    envelope.feedback ?? detail?.feedback ?? log.feedback,
  );
  const eligibleValue = envelope.feedback_eligible
    ?? detail?.feedback_eligible
    ?? log.feedback_eligible;
  const reason = text(
    envelope.feedback_ineligible_reason
      ?? detail?.feedback_ineligible_reason
      ?? log.feedback_ineligible_reason,
  ).trim();
  return {
    sourceRequestId: text(log.request_id).trim(),
    feedback,
    feedbackEligible: eligibleValue === true,
    feedbackIneligibleReason: reason || (feedback ? "already_recorded" : "attribution_unavailable"),
  };
}

/**
 * 创建 PAGE-070 独立 mutation memory。
 * 定时 RequestLog GET 不会 abort 这里的 POST；只有离页、切详情、身份变化、登出或显式取消才推进 mutation generation。
 */
export function createRequestFeedbackMutationMemory({
  requestIdFactory = createRequestFeedbackRequestId,
} = {}) {
  let activeIdentity = "";
  let activeNavigation = "";
  let sourceRequestId = "";
  let detailGeneration = 0;
  let formRevision = 0;
  let operationGeneration = 0;
  let ticket = null;
  let authoritativeFeedback = null;
  let feedbackEligible = false;
  let feedbackIneligibleReason = "";
  let rating = null;
  let accepted = null;
  let tags = [];
  let dirty = false;
  let actionError = null;
  let statusMessage = "";
  let retryOperation = null;

  /** 取消当前 POST；generation 才是迟到 fetch 忽略 AbortSignal 时的最终门禁。 */
  function abortMutation() {
    ticket?.controller.abort();
    ticket = null;
    operationGeneration += 1;
  }

  /** 清除可能属于上一租户或上一调用的反馈正文、草稿、错误和操作 ID。 */
  function clearDetailState() {
    authoritativeFeedback = null;
    feedbackEligible = false;
    feedbackIneligibleReason = "";
    rating = null;
    accepted = null;
    tags = [];
    dirty = false;
    actionError = null;
    statusMessage = "";
    retryOperation = null;
    formRevision += 1;
  }

  /** 进入一个完整详情 owner；身份、导航或源调用变化都会先销毁旧 mutation。 */
  function enter({ identity, navigation, nextSourceRequestId }) {
    const nextIdentity = text(identity);
    const nextNavigation = text(navigation);
    const normalizedSource = text(nextSourceRequestId).trim();
    if (!nextIdentity || nextNavigation !== "request-logs" || !normalizedSource) {
      leave();
      return false;
    }
    if (
      nextIdentity !== activeIdentity
      || nextNavigation !== activeNavigation
      || normalizedSource !== sourceRequestId
    ) {
      abortMutation();
      clearDetailState();
      activeIdentity = nextIdentity;
      activeNavigation = nextNavigation;
      sourceRequestId = normalizedSource;
      detailGeneration += 1;
    }
    return true;
  }

  /** 核对 mutation ticket 与实时页面 owner、详情代次和表单修订仍完全一致。 */
  function isCurrent(ticketValue, owner = {}) {
    return Boolean(
      ticketValue
      && ticket === ticketValue
      && !ticketValue.signal.aborted
      && ticketValue.identity === activeIdentity
      && ticketValue.navigation === activeNavigation
      && ticketValue.sourceRequestId === sourceRequestId
      && ticketValue.detailGeneration === detailGeneration
      && ticketValue.operationGeneration === operationGeneration
      && ticketValue.identity === text(owner.identity)
      && ticketValue.navigation === text(owner.navigation)
      && ticketValue.sourceRequestId === text(owner.sourceRequestId).trim(),
    );
  }

  /** 按固定白名单顺序规范化标签，确保请求意图和幂等指纹稳定。 */
  function normalizedTags() {
    return REQUEST_FEEDBACK_TAGS
      .map((item) => item.code)
      .filter((code) => tags.includes(code));
  }

  /** 重新计算 dirty；初始表单无默认采纳值，避免把未操作误记为 false。 */
  function refreshDirty() {
    dirty = rating !== null || accepted !== null || tags.length > 0;
    actionError = null;
    statusMessage = dirty ? "feedback.status.dirty" : "";
    formRevision += 1;
    // 表单意图变化后旧网络未知操作 ID 不再可复用，否则会触发后端 request_conflict。
    if (retryOperation?.fingerprint !== feedbackFingerprint()) {
      retryOperation = null;
    }
  }

  /** 生成客户端本地意图摘要；后端仍使用冻结的 SHA-256 v1 作为权威幂等指纹。 */
  function feedbackFingerprint() {
    return JSON.stringify([sourceRequestId, rating, accepted, normalizedTags()]);
  }

  /** 离开 PAGE-070 或认证 owner 消失时同步清除内存，不写入 storage 或 URL。 */
  function leave() {
    abortMutation();
    clearDetailState();
    activeIdentity = "";
    activeNavigation = "";
    sourceRequestId = "";
    detailGeneration += 1;
  }

  return {
    /**
     * 接纳当前详情的服务端资格与已有反馈。
     * 同一调用的普通 15 秒刷新只更新资格；dirty 或 busy 时绝不覆盖表单字段或取消 POST。
     */
    syncDetail({ identity, navigation, detail }) {
      const contract = requestFeedbackContractFromDetail(detail);
      if (!enter({ identity, navigation, nextSourceRequestId: contract.sourceRequestId })) {
        return false;
      }
      feedbackEligible = contract.feedbackEligible;
      feedbackIneligibleReason = contract.feedbackIneligibleReason;
      if (contract.feedback && !dirty && !ticket) {
        authoritativeFeedback = contract.feedback;
        feedbackEligible = false;
        feedbackIneligibleReason = "already_recorded";
      } else if (!contract.feedback && !dirty && !ticket) {
        authoritativeFeedback = null;
      }
      return true;
    },

    /** 选择 1..5 整数评分；bool、字符串和越界值都不能进入草稿。 */
    selectRating(value) {
      if (!Number.isSafeInteger(value) || value < 1 || value > 5 || ticket || authoritativeFeedback) {
        return false;
      }
      rating = value;
      refreshDirty();
      return true;
    },

    /** 选择明确的采纳事实；null 只表示初始未选择，不能被提交。 */
    selectAccepted(value) {
      if (typeof value !== "boolean" || ticket || authoritativeFeedback) {
        return false;
      }
      accepted = value;
      refreshDirty();
      return true;
    },

    /**
     * 切换固定标签并执行最多五个、唯一和互斥约束。
     * 返回 false 时调用方保留当前选择并展示浏览器级约束提示，后端仍会再次严格校验。
     */
    toggleTag(code, selected) {
      const normalized = text(code).trim();
      // 未知自由文本、非布尔选择、活动 POST 或已有事实都必须失败关闭，避免草稿绕过固定标签合同。
      if (!REQUEST_FEEDBACK_TAG_CODES.has(normalized) || typeof selected !== "boolean" || ticket || authoritativeFeedback) {
        return false;
      }
      const nextTags = tags.filter((item) => item !== normalized);
      if (selected) {
        if (nextTags.length >= MAX_FEEDBACK_TAGS || hasExclusiveTag(nextTags, normalized)) {
          return false;
        }
        nextTags.push(normalized);
      }
      tags = nextTags;
      refreshDirty();
      return true;
    },

    /**
     * 获取唯一 POST ticket；重复点击复用进行中的 ticket，网络结果未知的同意图重试复用原操作请求 ID。
     */
    acquireSubmit(owner = {}) {
      if (
        activeIdentity !== text(owner.identity)
        || activeNavigation !== text(owner.navigation)
        || sourceRequestId !== text(owner.sourceRequestId).trim()
      ) {
        return { state: "blocked", reason: "owner_changed" };
      }
      if (ticket) {
        return { state: "ok", ticket, reused: true };
      }
      const ready = feedbackEligible
        && !authoritativeFeedback
        && Number.isSafeInteger(rating)
        && rating >= 1
        && rating <= 5
        && typeof accepted === "boolean"
        && tags.length <= MAX_FEEDBACK_TAGS;
      if (!ready) {
        return { state: "blocked", reason: "form_incomplete" };
      }
      const fingerprint = feedbackFingerprint();
      const operationRequestId = retryOperation?.fingerprint === fingerprint
        ? retryOperation.operationRequestId
        : requestIdFactory();
      const controller = new AbortController();
      operationGeneration += 1;
      ticket = Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        sourceRequestId,
        detailGeneration,
        formRevision,
        operationGeneration,
        operationRequestId,
        fingerprint,
        rating,
        accepted,
        tags: Object.freeze(normalizedTags()),
        controller,
        signal: controller.signal,
      });
      actionError = null;
      statusMessage = "feedback.status.submitting";
      return { state: "ok", ticket, reused: false };
    },

    isCurrent,

    /** 接纳当前 POST 的 200/201；反馈必须与冻结源调用和表单意图精确一致。 */
    acceptSuccess(ticketValue, result, owner) {
      if (!isCurrent(ticketValue, owner)) {
        return false;
      }
      const feedback = projectFeedbackInfo(result?.feedback);
      if (
        !feedback
        || feedback.request_id !== ticketValue.sourceRequestId
        || feedback.rating !== ticketValue.rating
        || feedback.accepted !== ticketValue.accepted
        || JSON.stringify(feedback.tags) !== JSON.stringify(ticketValue.tags)
        || feedback.status !== "recorded"
      ) {
        return false;
      }
      ticket = null;
      authoritativeFeedback = feedback;
      feedbackEligible = false;
      feedbackIneligibleReason = "already_recorded";
      rating = null;
      accepted = null;
      tags = [];
      dirty = false;
      retryOperation = null;
      actionError = null;
      statusMessage = result?.idempotent_replay ? "feedback.status.confirmed" : "feedback.status.recorded";
      formRevision += 1;
      return true;
    },

    /**
     * 接纳当前 POST 的统一错误。
     * 网络错误和 5xx 结果未知时保留操作 ID；明确 4xx 则释放，避免不同意图误复用幂等键。
     */
    acceptFailure(ticketValue, error, owner) {
      if (!isCurrent(ticketValue, owner)) {
        return false;
      }
      ticket = null;
      actionError = safeFeedbackError(error, ticketValue.operationRequestId);
      statusMessage = "feedback.status.failed";
      retryOperation = !error?.status || error.status >= 500
        ? {
            fingerprint: ticketValue.fingerprint,
            operationRequestId: ticketValue.operationRequestId,
          }
        : null;
      return true;
    },

    /** 显式取消未提交草稿；进行中的 POST 会 abort，并通过 generation 阻止迟到响应回写。 */
    cancelDraft() {
      if (authoritativeFeedback) {
        return false;
      }
      abortMutation();
      rating = null;
      accepted = null;
      tags = [];
      dirty = false;
      actionError = null;
      statusMessage = "feedback.status.cleared";
      retryOperation = null;
      formRevision += 1;
      return true;
    },

    /** 返回不含 AbortController、Token 或内部指纹的渲染快照。 */
    snapshot() {
      const normalized = normalizedTags();
      return {
        sourceRequestId,
        detailGeneration,
        formRevision,
        authoritativeFeedback: authoritativeFeedback ? { ...authoritativeFeedback, tags: [...authoritativeFeedback.tags] } : null,
        feedbackEligible,
        feedbackIneligibleReason,
        rating,
        accepted,
        tags: normalized,
        dirty,
        busy: Boolean(ticket),
        canSubmit: Boolean(
          feedbackEligible
          && !authoritativeFeedback
          && !ticket
          && Number.isSafeInteger(rating)
          && rating >= 1
          && rating <= 5
          && typeof accepted === "boolean"
        ),
        actionError,
        statusMessage,
      };
    },

    leave,
  };
}

/**
 * 创建 PAGE-071 Summary GET 生命周期。
 * Summary 与 RequestLog/detail/POST 分别持有 AbortController，任一刷新都不能误取消其他链路。
 */
export function createRequestFeedbackSummaryMemory() {
  let activeIdentity = "";
  let activeNavigation = "";
  let activeQuery = "";
  let generation = 0;
  let controller = null;
  let ticket = null;
  let payload = null;
  let error = null;

  /** 取消当前 GET，并清除票据引用以防旧 finally 误完成新 owner。 */
  function abortRead() {
    controller?.abort();
    controller = null;
    ticket = null;
  }

  /** 判断 Summary 回调仍属于当前身份、导航、筛选和主刷新代次。 */
  function isCurrent(ticketValue, owner = {}) {
    return Boolean(
      ticketValue
      && ticket === ticketValue
      && !ticketValue.signal.aborted
      && ticketValue.generation === generation
      && ticketValue.identity === activeIdentity
      && ticketValue.navigation === activeNavigation
      && ticketValue.query === activeQuery
      && ticketValue.identity === text(owner.identity)
      && ticketValue.navigation === text(owner.navigation)
      && ticketValue.query === text(owner.query)
      && ticketValue.refreshId === owner.refreshId
      && ticketValue.refreshId === owner.currentRefreshId,
    );
  }

  return {
    /** 开始一轮 Summary GET；不完整身份或非活动页面直接返回 null，因此 viewer 零预取。 */
    beginRead({ identity, navigation, query, refreshId }) {
      const nextIdentity = text(identity);
      const nextNavigation = text(navigation);
      const nextQuery = text(query);
      if (!nextIdentity || nextNavigation !== "feedback-summary" || !nextQuery) {
        this.invalidate();
        return null;
      }
      abortRead();
      activeIdentity = nextIdentity;
      activeNavigation = nextNavigation;
      activeQuery = nextQuery;
      controller = new AbortController();
      ticket = Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        query: activeQuery,
        refreshId,
        generation: ++generation,
        controller,
        signal: controller.signal,
      });
      return ticket;
    },

    isCurrent,

    /** 接纳租户级 Summary 白名单 DTO；未知 scope 不得伪装为当前租户统计。 */
    acceptSuccess(ticketValue, result, owner) {
      if (!isCurrent(ticketValue, owner) || result?.scope_type !== "tenant" || !Array.isArray(result?.models)) {
        return false;
      }
      payload = result;
      error = null;
      controller = null;
      ticket = null;
      return true;
    },

    /** 保存当前 GET 的安全错误；不保留原始 URL、响应正文或 Header。 */
    acceptFailure(ticketValue, value, owner) {
      if (!isCurrent(ticketValue, owner)) {
        return false;
      }
      payload = null;
      error = safeFeedbackError(value, "-");
      controller = null;
      ticket = null;
      return true;
    },

    /** 返回报表稳定状态；首次 GET 完成前为 null，由渲染器显示加载占位。 */
    snapshot() {
      if (error) {
        return { state: "error", ...error };
      }
      return payload ? { ...payload, state: "ok" } : null;
    },

    /** 离页、登出、401、角色/租户/Session 变化时销毁全部 Summary owner。 */
    invalidate() {
      abortRead();
      activeIdentity = "";
      activeNavigation = "";
      activeQuery = "";
      payload = null;
      error = null;
      generation += 1;
    },
  };
}

/**
 * 渲染 PAGE-070 日志详情反馈区。
 * 编辑资格只采用后端 `feedback_eligible`；前端不会根据 status、stream 或路由尝试自行推断。
 */
export function renderRequestFeedbackSection(state, authContext, i18n) {
  if (!hasPermission(authContext, PERMISSIONS.FEEDBACK_USE) || !state?.sourceRequestId) {
    return "";
  }
  // 已有不可变事实时只渲染只读摘要，绝不生成覆盖或删除命令。
  if (state.authoritativeFeedback) {
    // 只把 client 白名单投影交给只读模板，未知后端字段不会进入 DOM。
    return renderRecordedFeedback(state.authoritativeFeedback, i18n, state.statusMessage);
  }
  if (!state.feedbackEligible) {
    return `
      <section class="request-feedback-section" aria-labelledby="request-feedback-title">
        <div class="request-feedback-heading">
          <h4 id="request-feedback-title">${escapeHtml(t(i18n, "feedback.form.title", "调用反馈"))}</h4>
          <span>${escapeHtml(t(i18n, "feedback.form.unavailable", "当前调用不可反馈"))}</span>
        </div>
        <p class="request-feedback-ineligible">${escapeHtml(ineligibleReasonLabel(state.feedbackIneligibleReason, i18n))}</p>
      </section>
    `;
  }
  const selectedTags = new Set(Array.isArray(state.tags) ? state.tags : []);
  const tagLimitReached = selectedTags.size >= MAX_FEEDBACK_TAGS;
  return `
    <section class="request-feedback-section" aria-labelledby="request-feedback-title">
      <div class="request-feedback-heading">
        <div>
          <h4 id="request-feedback-title">${escapeHtml(t(i18n, "feedback.form.title", "调用反馈"))}</h4>
          <code>${escapeHtml(state.sourceRequestId)}</code>
        </div>
        <span>${escapeHtml(`${selectedTags.size} / ${MAX_FEEDBACK_TAGS}`)}</span>
      </div>
      <form data-request-feedback-form aria-busy="${state.busy ? "true" : "false"}">
        <fieldset ${state.busy ? "disabled" : ""}>
          <legend>${escapeHtml(t(i18n, "feedback.form.rating", "评分"))}</legend>
          <div class="request-feedback-segments request-feedback-rating">
            ${[1, 2, 3, 4, 5].map((value) => renderRadioSegment({
              name: "rating",
              value: String(value),
              label: `${value}`,
              checked: state.rating === value,
            })).join("")}
          </div>
        </fieldset>
        <fieldset ${state.busy ? "disabled" : ""}>
          <legend>${escapeHtml(t(i18n, "feedback.form.accepted", "是否采纳"))}</legend>
          <div class="request-feedback-segments">
            ${renderRadioSegment({
              name: "accepted",
              value: "true",
              label: t(i18n, "feedback.accepted.yes", "已采纳"),
              checked: state.accepted === true,
            })}
            ${renderRadioSegment({
              name: "accepted",
              value: "false",
              label: t(i18n, "feedback.accepted.no", "未采纳"),
              checked: state.accepted === false,
            })}
          </div>
        </fieldset>
        <fieldset ${state.busy ? "disabled" : ""}>
          <legend>${escapeHtml(t(i18n, "feedback.form.tags", "标签"))}</legend>
          <div class="request-feedback-tags">
            ${REQUEST_FEEDBACK_TAGS.map((item) => renderTagCheckbox(
              item,
              selectedTags,
              tagLimitReached,
              i18n,
            )).join("")}
          </div>
        </fieldset>
        <div class="request-feedback-actions">
          <button type="submit" ${state.canSubmit ? "" : "disabled"}>${escapeHtml(
            state.busy
              ? t(i18n, "feedback.form.submitting", "提交中")
              : t(i18n, "feedback.form.submit", "提交反馈"),
          )}</button>
          <button type="button" data-request-feedback-cancel ${state.dirty || state.busy ? "" : "disabled"}>${escapeHtml(t(i18n, "feedback.form.cancel", "清除"))}</button>
        </div>
      </form>
      ${renderFeedbackStatus(state.actionError, state.statusMessage, i18n)}
    </section>
  `;
}

/**
 * 渲染 PAGE-071 租户反馈报表。
 * 总计只使用后端 summary，不从当前分页模型行重算；viewer 和未知角色返回零 DOM。
 */
export function renderRequestFeedbackSummaryPanel(state, authContext, filters = {}, i18n) {
  if (!hasPermission(authContext, PERMISSIONS.FEEDBACK_USE)) {
    return "";
  }
  if (!state) {
    return renderSummaryLoading(filters, i18n);
  }
  // 报表失败态只展示统一错误与 request_id，不回显 URL、Token、请求体或数据库诊断。
  if (state.state === "error") {
    return renderSummaryError(state, filters, i18n);
  }
  const summary = state.summary ?? {};
  const models = Array.isArray(state.models) ? state.models : [];
  return `
    ${renderSummaryHeading(state, i18n)}
    ${renderSummaryFilters(filters, i18n)}
    <div class="feedback-summary-metrics" aria-label="${escapeAttribute(t(i18n, "feedback.summary.metrics", "租户反馈总计"))}">
      ${renderMetric(t(i18n, "feedback.summary.samples", "反馈样本"), summary.feedback_count ?? 0)}
      ${renderMetric(t(i18n, "feedback.summary.average", "平均评分"), formatRatingMilli(summary.average_rating_milli))}
      ${renderMetric(t(i18n, "feedback.summary.adoption", "采纳率"), formatBps(summary.adoption_rate_bps))}
      ${renderMetric(t(i18n, "feedback.summary.accepted_count", "已采纳"), summary.accepted_count ?? 0)}
    </div>
    <div class="feedback-summary-breakdown">
      ${renderRatingDistribution(summary.rating_distribution, summary.feedback_count, i18n)}
      ${renderTagCounts(summary.tag_counts, i18n)}
    </div>
    ${renderModelSummaryTable(models, i18n)}
    ${renderSummaryPager(state, i18n)}
  `;
}

/** 渲染已有不可变反馈，只提供只读事实，不生成 PATCH/DELETE 命令。 */
function renderRecordedFeedback(feedback, i18n, statusMessage) {
  return `
    <section class="request-feedback-section request-feedback-recorded" aria-labelledby="request-feedback-title">
      <div class="request-feedback-heading">
        <h4 id="request-feedback-title">${escapeHtml(t(i18n, "feedback.form.recorded", "已记录反馈"))}</h4>
        <time datetime="${escapeAttribute(feedback.created_at)}">${escapeHtml(formatDateTime(feedback.created_at))}</time>
      </div>
      <dl class="request-feedback-facts">
        <div><dt>${escapeHtml(t(i18n, "feedback.form.rating", "评分"))}</dt><dd>${escapeHtml(formatRating(feedback.rating))}</dd></div>
        <div><dt>${escapeHtml(t(i18n, "feedback.form.accepted", "是否采纳"))}</dt><dd>${escapeHtml(acceptedLabel(feedback.accepted, i18n))}</dd></div>
        <div><dt>${escapeHtml(t(i18n, "feedback.form.model", "模型"))}</dt><dd><code>${escapeHtml(feedback.model_id || "-")}</code></dd></div>
      </dl>
      <div class="request-feedback-recorded-tags" aria-label="${escapeAttribute(t(i18n, "feedback.form.tags", "标签"))}">
        ${feedback.tags.length
          ? feedback.tags.map((tag) => `<span>${escapeHtml(tagLabel(tag, i18n))}</span>`).join("")
          : `<span>${escapeHtml(t(i18n, "feedback.tags.none", "无标签"))}</span>`}
      </div>
      ${statusMessage ? `<p class="request-feedback-live" role="status" tabindex="-1">${escapeHtml(statusLabel(statusMessage, i18n))}</p>` : ""}
    </section>
  `;
}

/** 渲染可访问 radio 分段项；input 保持可键盘操作，视觉选中由 :checked 控制。 */
function renderRadioSegment({ name, value, label, checked }) {
  return `
    <label>
      <input type="radio" name="${escapeAttribute(name)}" value="${escapeAttribute(value)}" ${checked ? "checked" : ""} required />
      <span>${escapeHtml(label)}</span>
    </label>
  `;
}

/** 渲染固定标签 checkbox；达到五个时只禁用未选项，不阻止用户取消已有选择。 */
function renderTagCheckbox(item, selectedTags, limitReached, i18n) {
  const selected = selectedTags.has(item.code);
  const disabled = !selected && (limitReached || hasExclusiveTag([...selectedTags], item.code));
  return `
    <label>
      <input type="checkbox" name="tags" value="${escapeAttribute(item.code)}" ${selected ? "checked" : ""} ${disabled ? "disabled" : ""} />
      <span>${escapeHtml(tagLabel(item.code, i18n))}</span>
    </label>
  `;
}

/** 当前标签集合命中目标标签的互斥项时返回 true。 */
function hasExclusiveTag(currentTags, candidate) {
  return REQUEST_FEEDBACK_EXCLUSIVE_TAGS.some(
    (group) => group.includes(candidate) && group.some((tag) => tag !== candidate && currentTags.includes(tag)),
  );
}

/** 渲染 POST 错误或 live 状态；只展示统一 code/message/request_id。 */
function renderFeedbackStatus(error, statusMessage, i18n) {
  if (error) {
    return `
      <div class="request-feedback-live error-state" role="alert" tabindex="-1">
        <strong>${escapeHtml(error.code || "feedback.unavailable")}</strong>
        <span>${escapeHtml(error.message || t(i18n, "feedback.form.error", "反馈提交失败"))}</span>
        <code>request_id: ${escapeHtml(error.requestId || "-")}</code>
      </div>
    `;
  }
  return statusMessage
    ? `<p class="request-feedback-live" role="status" aria-live="polite" tabindex="-1">${escapeHtml(statusLabel(statusMessage, i18n))}</p>`
    : `<p class="request-feedback-live" role="status" aria-live="polite"></p>`;
}

/** 渲染 Summary 加载态，同时保留筛选区的稳定尺寸。 */
function renderSummaryLoading(filters, i18n) {
  return `
    ${renderSummaryHeading(null, i18n)}
    ${renderSummaryFilters(filters, i18n)}
    <div class="feedback-summary-empty" role="status">${escapeHtml(t(i18n, "feedback.summary.loading", "正在读取当前租户反馈统计"))}</div>
  `;
}

/** 渲染 Summary 错误态，不回显 URL、Token、请求正文或 SQL。 */
function renderSummaryError(state, filters, i18n) {
  return `
    ${renderSummaryHeading(null, i18n)}
    ${renderSummaryFilters(filters, i18n)}
    <div class="feedback-summary-empty error-state" role="alert">
      <strong>${escapeHtml(state.code || "feedback.unavailable")}</strong>
      <span>${escapeHtml(state.message || t(i18n, "feedback.summary.error", "反馈报表暂不可用"))}</span>
      <code>request_id: ${escapeHtml(state.requestId || "-")}</code>
    </div>
  `;
}

/** 渲染 Summary 标题和后端权威时间窗；没有响应时不伪造本地窗口。 */
function renderSummaryHeading(state, i18n) {
  const scope = state?.scope_type === "tenant"
    ? t(i18n, "feedback.summary.tenant_scope", "当前租户")
    : t(i18n, "feedback.summary.pending_scope", "口径待加载");
  return `
    <div class="feedback-summary-heading">
      <div>
        <h2 id="feedback-summary-title">${escapeHtml(t(i18n, "feedback.summary.title", "反馈报表"))}</h2>
        <p>${escapeHtml(state?.from_time && state?.to_time ? `${state.from_time} - ${state.to_time}` : scope)}</p>
      </div>
      <span>${escapeHtml(scope)}</span>
    </div>
  `;
}

/** 渲染模型和 UTC 时间筛选；表单不包含 tenant_id 或 actor 输入。 */
function renderSummaryFilters(filters, i18n) {
  return `
    <form class="feedback-summary-filters" id="feedback-summary-filter-form">
      <label><span>${escapeHtml(t(i18n, "feedback.summary.model", "模型 ID"))}</span><input name="modelId" maxlength="128" value="${escapeAttribute(filters.modelId ?? "")}" /></label>
      <label><span>${escapeHtml(t(i18n, "feedback.summary.from", "开始时间（UTC）"))}</span><input name="fromTime" maxlength="20" placeholder="2026-06-19T12:00:00Z" value="${escapeAttribute(filters.fromTime ?? "")}" /></label>
      <label><span>${escapeHtml(t(i18n, "feedback.summary.to", "结束时间（UTC）"))}</span><input name="toTime" maxlength="20" placeholder="2026-07-19T12:00:00Z" value="${escapeAttribute(filters.toTime ?? "")}" /></label>
      <div class="feedback-summary-filter-actions">
        <button type="submit">${escapeHtml(t(i18n, "feedback.summary.apply", "查询"))}</button>
        <button type="button" id="feedback-summary-refresh">${escapeHtml(t(i18n, "feedback.summary.refresh", "刷新"))}</button>
        <button type="button" id="feedback-summary-reset">${escapeHtml(t(i18n, "feedback.summary.reset", "清空"))}</button>
      </div>
    </form>
  `;
}

/** 渲染一项服务端权威总计，不在浏览器重新聚合分页行。 */
function renderMetric(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

/** 渲染五档评分分布；progress 只使用已白名单投影的非负整数。 */
function renderRatingDistribution(distribution, total, i18n) {
  const safeTotal = Number.isSafeInteger(total) && total > 0 ? total : 1;
  return `
    <section class="feedback-summary-distribution" aria-labelledby="feedback-rating-distribution-title">
      <h3 id="feedback-rating-distribution-title">${escapeHtml(t(i18n, "feedback.summary.distribution", "评分分布"))}</h3>
      ${[5, 4, 3, 2, 1].map((ratingValue) => {
        const count = distribution?.[String(ratingValue)] ?? 0;
        return `<div><span>${escapeHtml(`${ratingValue} / 5`)}</span><progress max="${escapeAttribute(safeTotal)}" value="${escapeAttribute(count)}"></progress><strong>${escapeHtml(count)}</strong></div>`;
      }).join("")}
    </section>
  `;
}

/** 渲染完整窗口的标签计数；标签显示用固定字典，未知 code 仍转义后展示以便排障。 */
function renderTagCounts(tagCounts, i18n) {
  const rows = Array.isArray(tagCounts) ? tagCounts : [];
  return `
    <section class="feedback-summary-tags" aria-labelledby="feedback-tag-counts-title">
      <h3 id="feedback-tag-counts-title">${escapeHtml(t(i18n, "feedback.summary.tags", "标签统计"))}</h3>
      <div>
        ${rows.length
          ? rows.map((item) => `<span><em>${escapeHtml(tagLabel(item.tag, i18n))}</em><strong>${escapeHtml(item.count)}</strong></span>`).join("")
          : `<p>${escapeHtml(t(i18n, "feedback.summary.no_tags", "当前窗口没有标签"))}</p>`}
      </div>
    </section>
  `;
}

/** 渲染按模型分页表；窄屏由 data-label 转换为单列记录。 */
function renderModelSummaryTable(models, i18n) {
  return `
    <div class="feedback-model-table" role="table" aria-label="${escapeAttribute(t(i18n, "feedback.summary.models", "按模型反馈统计"))}">
      <div class="feedback-model-row feedback-model-head" role="row">
        <span>${escapeHtml(t(i18n, "feedback.summary.model", "模型"))}</span><span>${escapeHtml(t(i18n, "feedback.summary.samples", "反馈样本"))}</span><span>${escapeHtml(t(i18n, "feedback.summary.average", "平均评分"))}</span><span>${escapeHtml(t(i18n, "feedback.summary.accepted_count", "已采纳"))}</span><span>${escapeHtml(t(i18n, "feedback.summary.adoption", "采纳率"))}</span><span>${escapeHtml(t(i18n, "feedback.summary.distribution", "评分分布"))}</span>
      </div>
      ${models.length ? models.map((model) => `
        <div class="feedback-model-row" role="row">
          <span data-label="${escapeAttribute(t(i18n, "feedback.summary.model", "模型"))}"><strong>${escapeHtml(model.display_name || model.internal_model_id || model.model_id || "-")}</strong><code>${escapeHtml(model.internal_model_id || model.model_id || "-")}</code></span>
          <span data-label="${escapeAttribute(t(i18n, "feedback.summary.samples", "反馈样本"))}">${escapeHtml(model.feedback_count)}</span>
          <span data-label="${escapeAttribute(t(i18n, "feedback.summary.average", "平均评分"))}">${escapeHtml(formatRatingMilli(model.average_rating_milli))}</span>
          <span data-label="${escapeAttribute(t(i18n, "feedback.summary.accepted_count", "已采纳"))}">${escapeHtml(model.accepted_count)}</span>
          <span data-label="${escapeAttribute(t(i18n, "feedback.summary.adoption", "采纳率"))}">${escapeHtml(formatBps(model.adoption_rate_bps))}</span>
          <code data-label="${escapeAttribute(t(i18n, "feedback.summary.distribution", "评分分布"))}">${escapeHtml(compactDistribution(model.rating_distribution))}</code>
        </div>
      `).join("") : `<div class="feedback-model-empty" role="row">${escapeHtml(t(i18n, "feedback.summary.empty", "当前筛选没有反馈样本"))}</div>`}
    </div>
  `;
}

/** 渲染 Summary 分页命令；offset 和 limit 已由 client 白名单投影为安全整数。 */
function renderSummaryPager(state, i18n) {
  const offset = state.offset ?? 0;
  const limit = state.limit ?? 20;
  const total = state.total ?? 0;
  return `
    <div class="feedback-summary-pager">
      <button type="button" id="feedback-summary-prev" ${offset <= 0 ? "disabled" : ""}>${escapeHtml(t(i18n, "feedback.summary.prev", "上一页"))}</button>
      <span>${escapeHtml(total ? `${offset + 1}-${Math.min(offset + limit, total)} / ${total}` : "0 / 0")}</span>
      <button type="button" id="feedback-summary-next" ${offset + limit >= total ? "disabled" : ""}>${escapeHtml(t(i18n, "feedback.summary.next", "下一页"))}</button>
    </div>
  `;
}

/** 将详情不可反馈原因码转换为展示文案；资格结论仍完全来自后端。 */
function ineligibleReasonLabel(reason, i18n) {
  const labels = {
    already_recorded: t(i18n, "feedback.reason.already_recorded", "该调用已记录反馈"),
    request_failed: t(i18n, "feedback.reason.request_failed", "失败调用不能提交反馈"),
    stream_incomplete: t(i18n, "feedback.reason.stream_incomplete", "流式调用尚未完成"),
    attribution_unavailable: t(i18n, "feedback.reason.attribution_unavailable", "最终模型归因暂不可用"),
  };
  return labels[reason] ?? reason ?? t(i18n, "feedback.form.unavailable", "当前调用不可反馈");
}

/** 固定标签 code 转为当前语言；未知 code 保留原值并由模板统一转义。 */
function tagLabel(code, i18n) {
  const fallback = REQUEST_FEEDBACK_TAGS.find((item) => item.code === code)?.label ?? code ?? "-";
  return t(i18n, `feedback.tag.${code}`, fallback);
}

/** 采纳布尔值使用明确文案；null 不得伪装成“未采纳”。 */
function acceptedLabel(value, i18n) {
  if (value === true) return t(i18n, "feedback.accepted.yes", "已采纳");
  if (value === false) return t(i18n, "feedback.accepted.no", "未采纳");
  return "-";
}

/** 1..5 评分展示固定分母，异常值保持短横线。 */
function formatRating(value) {
  return Number.isSafeInteger(value) && value >= 1 && value <= 5 ? `${value} / 5` : "-";
}

/** 将整数 milli 格式化为最多三位小数，不使用浮点重新决定权威值。 */
function formatRatingMilli(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    return "-";
  }
  const whole = Math.floor(value / 1000);
  const fraction = String(value % 1000).padStart(3, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""} / 5`;
}

/** 将整数 bps 格式化为百分比，不用 JavaScript 浮点重算采纳率。 */
function formatBps(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    return "-";
  }
  const whole = Math.floor(value / 100);
  const fraction = String(value % 100).padStart(2, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}%`;
}

/** 将五档分布压缩为稳定文本，便于窄表格扫描且不丢零计数。 */
function compactDistribution(value) {
  return [1, 2, 3, 4, 5].map((ratingValue) => `${ratingValue}:${value?.[String(ratingValue)] ?? 0}`).join(" / ");
}

/** ISO 时间只做长度收敛，不进行本地时区重算。 */
function formatDateTime(value) {
  return value ? text(value).replace("T", " ").replace(/\.\d+Z?$/, "Z") : "-";
}

/** 错误内存只保留统一公开字段，不能把响应正文、Header、URL 或异常栈传给渲染器。 */
function safeFeedbackError(error, fallbackRequestId) {
  return {
    status: Number.isSafeInteger(error?.status) ? error.status : null,
    code: text(error?.code) || "feedback.unavailable",
    message: text(error?.message) || "反馈请求未完成",
    requestId: text(error?.requestId ?? error?.request_id ?? fallbackRequestId) || "-",
  };
}

/** 把内存中的稳定状态 key 转为当前语言；未知值原样展示，兼容测试和并行旧状态。 */
function statusLabel(value, i18n) {
  const normalized = text(value);
  return normalized.startsWith("feedback.status.") ? t(i18n, normalized, normalized) : normalized;
}

/** 读取 i18n 文案，缺失上下文时保留中文 fallback。 */
function t(i18n, key, fallback) {
  return i18n?.t?.(key, {}, fallback) ?? fallback;
}

/** 将可空字段收敛为文本，不序列化对象或数组。 */
function text(value) {
  return value === null || value === undefined ? "" : String(value);
}

/** HTML 文本转义，防止模型名、错误码、request_id 或未知枚举破坏页面结构。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** HTML 属性转义额外处理反引号，保护 value、data-* 和 aria 属性。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
