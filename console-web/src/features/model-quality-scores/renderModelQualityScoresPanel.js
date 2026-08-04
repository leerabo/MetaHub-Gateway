/**
 * 模块职责：渲染 PAGE-069 模型评分页，并维护按身份、查询和模型隔离的人工评分草稿。
 * 安全边界：viewer 不生成 DOM；前端不提交 tenant_id/统计字段，后端 RBAC 与 CAS 始终是最终权限边界。
 */

import { createModelQualityScoreRequestId } from "../../lib/modelQualityScores.js";
import {
  authorizationIdentitySnapshot,
  hasPermission,
  PERMISSIONS,
} from "../../lib/permissions.js";

/**
 * 从认证上下文构造 PAGE-069 owner 快照。
 * tenant、用户、角色、Session 或 revision 任一变化都会使旧草稿和迟到响应失效。
 */
export function modelQualityScoreIdentitySnapshot(authContext) {
  // owner 绑定后端读权限 revision/fingerprint，权限撤销后旧草稿和迟到响应立即失效。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.MODEL_QUALITY_READ);
}

/**
 * 把百分比文本精确转换为 0..10000 bps。
 * 使用字符串拆分而不是浮点乘法，确保 83.55% 稳定得到 8355。
 */
export function qualityPercentToBps(value) {
  const normalized = text(value).trim();
  if (!/^(?:100(?:\.0{1,2})?|\d{1,2}(?:\.\d{1,2})?)$/.test(normalized)) {
    return null;
  }
  const [whole, fraction = ""] = normalized.split(".");
  const bps = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return Number.isSafeInteger(bps) && bps >= 0 && bps <= 10000 ? bps : null;
}

/**
 * 创建 PAGE-069 纯内存状态仓库。
 * refresh generation 只淘汰 GET，operation generation 只淘汰保存，避免 15 秒刷新吞掉合法 PATCH。
 */
export function createModelQualityScorePageMemory({ requestIdFactory } = {}) {
  const createRequestId = requestIdFactory ?? createModelQualityScoreRequestId;
  const rows = new Map();
  let activeIdentity = "";
  let activeNavigation = "";
  let activeQuery = "";
  let refreshGeneration = 0;
  let refreshController = null;
  let loadError = null;
  let pageMeta = { total: 0, limit: 100, offset: 0 };

  /** 身份、页面或查询变化时取消旧 ticket，分页切换不能保留另一结果集的可写草稿。 */
  function enter(identity, navigation, query = activeQuery) {
    const nextIdentity = text(identity);
    const nextNavigation = text(navigation);
    const nextQuery = text(query);
    if (
      activeIdentity === nextIdentity
      && activeNavigation === nextNavigation
      && activeQuery === nextQuery
    ) {
      return;
    }
    invalidateRows();
    activeIdentity = nextIdentity;
    activeNavigation = nextNavigation;
    activeQuery = nextQuery;
  }

  /** abort 全部写请求并推进双代次，确保 fetch 忽略 Abort 时迟到回调仍被 owner 门禁拒绝。 */
  function invalidateRows() {
    refreshController?.abort();
    refreshController = null;
    for (const row of rows.values()) {
      row.ticket?.controller.abort();
      row.ticket = null;
      row.operationGeneration += 1;
    }
    rows.clear();
    refreshGeneration += 1;
    loadError = null;
    pageMeta = { total: 0, limit: 100, offset: 0 };
  }

  /** 将 GET/PATCH Score 采用为权威值，自动统计变化不会覆盖尚未明确取消的人工评分草稿。 */
  function adoptAuthority(row, score) {
    const authoritative = copyScore(score);
    row.authoritative = authoritative;
    row.baseFeedbackVersion = validVersion(authoritative.feedback_version);
    row.draftManualQualityBps = validOptionalBps(authoritative.manual_quality_bps);
    row.draftRevision += 1;
    row.dirty = false;
    row.conflicted = false;
    row.sideAuthoritativeSnapshot = null;
    row.retryOperation = null;
  }

  /** 为当前页第一次出现的 Model 建立独立行状态。 */
  function createRow(score) {
    const authoritative = copyScore(score);
    return {
      modelId: text(authoritative.model_id),
      scoreId: text(authoritative.model_quality_score_id),
      authoritative,
      baseFeedbackVersion: validVersion(authoritative.feedback_version),
      draftManualQualityBps: validOptionalBps(authoritative.manual_quality_bps),
      draftRevision: 0,
      dirty: false,
      conflicted: false,
      sideAuthoritativeSnapshot: null,
      operationGeneration: 0,
      ticket: null,
      retryOperation: null,
      actionError: null,
      feedback: "",
    };
  }

  /** 核对 GET ticket 仍属于当前身份、导航、查询和最新读取代次。 */
  function isCurrentRefresh(ticket) {
    return Boolean(
      ticket
      && !ticket.signal.aborted
      && ticket.generation === refreshGeneration
      && ticket.identity === activeIdentity
      && ticket.navigation === activeNavigation
      && ticket.query === activeQuery,
    );
  }

  /** 核对 PATCH ticket 仍属于当前 owner 和同一模型行；GET 代次故意不参与。 */
  function isCurrentOperation(ticket, owner = {}) {
    if (!ticket || ticket.identity !== activeIdentity || ticket.navigation !== activeNavigation) {
      return false;
    }
    const row = rows.get(ticket.modelId);
    return Boolean(
      row
      && row.ticket === ticket
      && !ticket.signal.aborted
      && ticket.operationGeneration === row.operationGeneration
      && ticket.identity === text(owner.identity ?? activeIdentity)
      && ticket.navigation === text(owner.navigation ?? activeNavigation),
    );
  }

  return {
    /** 开始当前页 GET；相同查询的刷新不会推进任何人工评分保存代次。 */
    beginRefresh({ identity, navigation, query }) {
      enter(identity, navigation, query);
      // 同一查询的新刷新会淘汰旧 GET，但不会推进任何行级 PATCH operation generation。
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

    /** 接纳最新 GET；dirty/busy/conflict 行只保存旁路快照，防止自动统计刷新覆盖人工草稿。 */
    acceptRefresh(ticket, payload) {
      if (!isCurrentRefresh(ticket)) {
        return false;
      }
      const seen = new Set();
      for (const score of Array.isArray(payload?.model_quality_scores) ? payload.model_quality_scores : []) {
        const modelId = text(score?.model_id);
        if (!modelId) {
          continue;
        }
        seen.add(modelId);
        const row = rows.get(modelId);
        if (!row) {
          rows.set(modelId, createRow(score));
        } else if (row.dirty || row.ticket || row.conflicted) {
          row.sideAuthoritativeSnapshot = copyScore(score);
        } else {
          adoptAuthority(row, score);
        }
      }
      // 当前分页中消失的 clean 行直接移除；编辑中行保留到用户取消或后端 PATCH 明确返回 404。
      for (const [modelId, row] of rows) {
        if (!seen.has(modelId) && !row.dirty && !row.ticket && !row.conflicted) {
          rows.delete(modelId);
        }
      }
      pageMeta = {
        total: nonnegativeInteger(payload?.total),
        limit: positiveInteger(payload?.limit, 100),
        offset: nonnegativeInteger(payload?.offset),
      };
      if (refreshController === ticket.controller) {
        refreshController = null;
      }
      loadError = null;
      return true;
    },

    /** 保存最新 GET 的统一错误；已有草稿继续留在页面，首次读取失败才进入致命错误态。 */
    acceptRefreshError(ticket, error) {
      if (!isCurrentRefresh(ticket)) {
        return false;
      }
      loadError = error;
      if (refreshController === ticket.controller) {
        refreshController = null;
      }
      return true;
    },

    /** 更新一个模型的人工评分草稿；null 表示显式清除，切换不会立即调用后端。 */
    selectManualQuality(modelId, manualQualityBps) {
      const row = rows.get(text(modelId));
      const normalized = validOptionalBps(manualQualityBps);
      if (!row || row.ticket || row.conflicted || normalized === undefined) {
        return false;
      }
      row.draftManualQualityBps = normalized;
      row.draftRevision += 1;
      row.dirty = normalized !== validOptionalBps(row.authoritative.manual_quality_bps);
      row.actionError = null;
      row.feedback = row.dirty ? "存在未保存人工评分" : "";
      if (!row.dirty) {
        row.retryOperation = null;
      }
      return true;
    },

    /** 获取唯一保存 ticket；重复点击不再发 PATCH，结果未知重试复用原 request_id。 */
    acquireSave({ identity, navigation, modelId }) {
      enter(identity, navigation);
      const row = rows.get(text(modelId));
      if (!row) {
        return { state: "blocked", reason: "missing_model" };
      }
      if (row.ticket) {
        return { state: "ok", ticket: row.ticket, reused: true };
      }
      if (!row.dirty || row.conflicted || !row.scoreId || !validVersion(row.baseFeedbackVersion)) {
        return { state: "blocked", reason: row.conflicted ? "conflict" : "not_ready" };
      }
      row.operationGeneration += 1;
      const fingerprint = JSON.stringify([
        row.scoreId,
        row.modelId,
        row.draftManualQualityBps,
        row.baseFeedbackVersion,
      ]);
      const requestId = row.retryOperation?.fingerprint === fingerprint
        ? row.retryOperation.requestId
        : createRequestId();
      const controller = new AbortController();
      row.ticket = Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        modelId: row.modelId,
        scoreId: row.scoreId,
        manualQualityBps: row.draftManualQualityBps,
        baseFeedbackVersion: row.baseFeedbackVersion,
        draftRevision: row.draftRevision,
        operationGeneration: row.operationGeneration,
        fingerprint,
        requestId,
        controller,
        signal: controller.signal,
      });
      row.feedback = "保存中";
      row.actionError = null;
      return { state: "ok", ticket: row.ticket, reused: false };
    },

    /** 接纳属于当前 owner 的 PATCH 200；统计版本可前进，但人工评分事实必须与 ticket 精确一致。 */
    acceptSuccess(ticket, result, owner) {
      if (!isCurrentOperation(ticket, owner)) {
        return false;
      }
      const row = rows.get(ticket.modelId);
      const score = result?.model_quality_score;
      if (
        result?.changed !== true
        || !score
        || text(score.model_id) !== ticket.modelId
        || text(score.model_quality_score_id) !== ticket.scoreId
        || validOptionalBps(score.manual_quality_bps) !== ticket.manualQualityBps
        || validVersion(score.feedback_version) !== ticket.baseFeedbackVersion + 1
      ) {
        return false;
      }
      row.ticket = null;
      adoptAuthority(row, score);
      row.feedback = "人工评分已保存";
      row.actionError = null;
      return true;
    },

    /** 处理 409、确定失败或网络结果未知；409 必须显式 rebase，5xx/网络失败保留幂等键。 */
    acceptFailure(ticket, error, owner) {
      if (!isCurrentOperation(ticket, owner)) {
        return false;
      }
      const row = rows.get(ticket.modelId);
      row.ticket = null;
      row.actionError = error;
      row.feedback = error?.status === 409 ? "评分版本已变化，请载入最新版本" : "人工评分保存失败";
      // CAS 冲突冻结原草稿，只有显式 rebase 才能采用旁路权威值并解除门禁。
      if (error?.status === 409) {
        row.conflicted = true;
        const current = error?.currentModelQualityScore;
        // 错误体只能为同一模型和 Score 提供旁路权威值，其他对象不得跨行覆盖。
        if (
          current
          && text(current.model_id) === row.modelId
          && text(current.model_quality_score_id) === row.scoreId
        ) {
          row.sideAuthoritativeSnapshot = copyScore(current);
        }
        row.retryOperation = null;
      } else if (!error?.status || error.status >= 500) {
        row.retryOperation = { fingerprint: ticket.fingerprint, requestId: ticket.requestId };
      } else {
        row.retryOperation = null;
      }
      return true;
    },

    /** 明确取消当前草稿；若刷新已观察到新统计/版本，优先采用旁路权威值。 */
    cancel(modelId) {
      const row = rows.get(text(modelId));
      if (!row) {
        return false;
      }
      row.ticket?.controller.abort();
      row.ticket = null;
      row.operationGeneration += 1;
      // 刷新已观察到更新版本时，取消必须采用新事实，不能把过期评分重新标记为 clean。
      if (row.sideAuthoritativeSnapshot) {
        adoptAuthority(row, row.sideAuthoritativeSnapshot);
        row.feedback = "已取消并载入最新评分";
        row.actionError = null;
        return true;
      }
      if (row.conflicted) {
        row.feedback = "正在读取最新评分";
        row.actionError = null;
        return true;
      }
      row.draftManualQualityBps = validOptionalBps(row.authoritative.manual_quality_bps);
      row.draftRevision += 1;
      row.dirty = false;
      row.retryOperation = null;
      row.feedback = "已取消未保存评分";
      row.actionError = null;
      return true;
    },

    /** 管理员显式采用 409/刷新得到的旁路事实；采用后必须重新编辑才能再次保存。 */
    rebase(modelId) {
      const row = rows.get(text(modelId));
      if (!row || row.ticket || !row.sideAuthoritativeSnapshot) {
        return false;
      }
      adoptAuthority(row, row.sideAuthoritativeSnapshot);
      row.feedback = "已载入最新评分，请重新确认";
      row.actionError = null;
      return true;
    },

    /** 返回不含 AbortController、Token 或 Session 明文的渲染快照。 */
    snapshot() {
      const renderedRows = [...rows.values()].map((row) => ({
        modelId: row.modelId,
        scoreId: row.scoreId,
        score: copyScore(row.authoritative),
        baseFeedbackVersion: row.baseFeedbackVersion,
        draftManualQualityBps: row.draftManualQualityBps,
        draftRevision: row.draftRevision,
        dirty: row.dirty,
        busy: Boolean(row.ticket),
        conflicted: row.conflicted,
        sideAuthoritativeSnapshot: row.sideAuthoritativeSnapshot
          ? copyScore(row.sideAuthoritativeSnapshot)
          : null,
        operationGeneration: row.operationGeneration,
        actionError: row.actionError,
        feedback: row.feedback,
      }));
      return {
        state: loadError && !renderedRows.length ? "error" : "ok",
        rows: renderedRows,
        refreshError: renderedRows.length ? loadError : null,
        ...pageMeta,
      };
    },

    /** 离页、登出、401、pagehide 或身份变化时销毁全部 PAGE-069 状态。 */
    invalidate() {
      invalidateRows();
      activeIdentity = "";
      activeNavigation = "";
      activeQuery = "";
    },

    /** 暴露写 owner 门禁，供主应用在每个异步回调前复核。 */
    isCurrentOperation,

    /** 暴露读取 owner 门禁；迟到 401/403 只有仍属于当前身份、查询和导航时才可影响会话。 */
    isCurrentRefresh,
  };
}

/**
 * 渲染 PAGE-069 模型评分页。
 * admin 可维护人工评分，developer 只读；viewer 和未知角色返回空字符串。
 */
export function renderModelQualityScoresPanel(state, authContext, filters = {}) {
  // 缺少后端模型质量读权限时连筛选和分页 DOM 都不生成。
  if (!hasPermission(authContext, PERMISSIONS.MODEL_QUALITY_READ)) {
    return "";
  }
  if (!state) {
    return renderLoading();
  }
  // 首次读取失败时没有可信 Score/version，整个页面失败关闭而不是提供空编辑器。
  if (state.state === "error") {
    return renderFatalError(state.refreshError ?? state);
  }
  const rows = Array.isArray(state.rows) ? state.rows : [];
  const canWrite = hasPermission(authContext, PERMISSIONS.MODEL_QUALITY_WRITE);
  return `
    <div class="model-quality-heading">
      <div>
        <h2 id="model-quality-scores-title">模型评分</h2>
        <p>当前租户模型的成功率、延迟和人工质量先验。</p>
      </div>
      <span>${escapeHtml(rows.length)} / ${escapeHtml(state.total ?? rows.length)}</span>
    </div>
    ${renderRefreshError(state.refreshError)}
    ${renderFilterForm(filters)}
    <div class="model-quality-table" role="table" aria-label="当前租户模型评分">
      <div class="model-quality-row model-quality-head${canWrite ? "" : " model-quality-readonly-row"}" role="row">
        <span>模型 / Provider</span><span>调用样本</span><span>成功 / 错误</span><span>平均延迟</span><span>人工 / 有效评分</span><span>逐请求反馈</span><span>版本</span>${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${rows.length ? rows.map((row) => renderScoreRow(row, canWrite)).join("") : renderEmptyRow()}
    </div>
    ${renderPager(state)}
  `;
}

/** 渲染模型过滤条件；查询只发送 model_id 和分页，不提供 tenant_id 输入。 */
function renderFilterForm(filters) {
  return `
    <form class="model-quality-filter" id="model-quality-filter-form">
      <label><span>模型 ID</span><input name="modelId" maxlength="128" value="${escapeAttribute(filters.modelId ?? "")}" /></label>
      <div class="model-quality-filter-actions">
        <button type="submit">查询</button>
        <button type="button" id="model-quality-filter-reset">清空</button>
      </div>
    </form>
  `;
}

/** 渲染一条冻结统计和人工评分编辑器；所有服务端文本统一转义。 */
function renderScoreRow(row, canWrite) {
  const score = row.score ?? {};
  const sideVersion = validVersion(row.sideAuthoritativeSnapshot?.feedback_version);
  return `
    <div class="model-quality-row${canWrite ? "" : " model-quality-readonly-row"}${row.busy ? " is-busy" : ""}${row.conflicted ? " is-conflicted" : ""}" role="row" data-model-quality-row="${escapeAttribute(row.modelId)}">
      <span class="model-quality-identity" data-label="模型 / Provider">
        <strong>${escapeHtml(score.display_name ?? score.internal_model_id ?? row.modelId)}</strong>
        <code>${escapeHtml(score.internal_model_id ?? row.modelId)}</code>
        <em>${escapeHtml(score.provider_name ?? score.provider_code ?? "-")}</em>
      </span>
      <span data-label="调用样本"><strong>${escapeHtml(score.sample_count ?? 0)}</strong><em>${escapeHtml(observationLabel(score.observation_status))}</em></span>
      <span data-label="成功 / 错误"><strong>${escapeHtml(formatBps(score.success_rate_bps))}</strong><em>${escapeHtml(formatBps(score.error_rate_bps))}</em></span>
      <span data-label="平均延迟"><strong>${escapeHtml(formatLatency(score.average_latency_ms))}</strong><em>${escapeHtml(score.latency_sample_count ?? 0)} 个样本</em></span>
      <span data-label="人工 / 有效评分"><strong>${escapeHtml(formatManualQuality(score.manual_quality_bps))}</strong><em>${escapeHtml(formatBps(score.effective_quality_bps))}</em></span>
      ${renderRequestFeedbackStatistics(score)}
      <span data-label="版本"><strong>反馈 v${escapeHtml(score.feedback_version ?? "-")}</strong><em>统计 v${escapeHtml(score.statistics_version ?? "-")}</em>${sideVersion && sideVersion !== row.baseFeedbackVersion ? `<small>服务器已有 v${escapeHtml(sideVersion)}</small>` : ""}</span>
      ${canWrite ? `<span class="model-quality-action" data-label="操作">${renderScoreEditor(row)}</span>` : ""}
      ${renderRowFeedback(row)}
    </div>
  `;
}

/**
 * 渲染 PRD-049 独立反馈维度。
 * 该维度只展示后端聚合值，不能与 PRD-048 的调用终态样本、人工先验或路由公式版本相加或互相推导。
 */
function renderRequestFeedbackStatistics(score) {
  const count = nonnegativeInteger(score.request_feedback_count);
  const accepted = nonnegativeInteger(score.request_feedback_accepted_count);
  const version = nonnegativeInteger(score.request_feedback_statistics_version);
  return `
    <span class="model-quality-request-feedback" data-label="逐请求反馈">
      <strong>${escapeHtml(formatRatingMilli(score.request_feedback_average_milli))}</strong>
      <em>${escapeHtml(count)} 条 / 已采纳 ${escapeHtml(accepted)} / ${escapeHtml(formatBps(score.request_feedback_adoption_rate_bps))}</em>
      <small>分布 ${escapeHtml(formatRequestFeedbackDistribution(score.request_feedback_rating_distribution))} / v${escapeHtml(version)}</small>
    </span>
  `;
}

/** 渲染 admin 行级编辑器；清除按钮只修改草稿，保存仍需显式提交。 */
function renderScoreEditor(row) {
  const cannotSave = !row.dirty
    || row.busy
    || row.conflicted
    || !row.scoreId
    || !validVersion(row.baseFeedbackVersion);
  return `
    <form data-model-quality-form data-model-id="${escapeAttribute(row.modelId)}" aria-busy="${row.busy ? "true" : "false"}">
      <label>
        <span>人工评分（%）</span>
        <input name="manualQualityPercent" inputmode="decimal" type="number" min="0" max="100" step="0.01" value="${escapeAttribute(bpsInputValue(row.draftManualQualityBps))}" ${row.busy || row.conflicted ? "disabled" : ""} />
      </label>
      <div class="model-quality-buttons">
        <button type="submit" ${cannotSave ? "disabled" : ""}>${row.busy ? "保存中" : "保存"}</button>
        <button type="button" data-model-quality-clear ${row.busy || row.conflicted || row.draftManualQualityBps === null ? "disabled" : ""}>清除评分</button>
        <button type="button" data-model-quality-cancel ${!row.dirty && !row.busy && !row.conflicted ? "disabled" : ""}>取消</button>
        ${row.conflicted && row.sideAuthoritativeSnapshot ? `<button type="button" data-model-quality-rebase>载入最新版本</button>` : ""}
      </div>
    </form>
  `;
}

/** 渲染行级统一错误或 live feedback，不展示响应正文和未知字段。 */
function renderRowFeedback(row) {
  if (row.actionError) {
    return `
      <div class="model-quality-row-feedback error-state" role="alert" tabindex="-1">
        <strong>${escapeHtml(row.actionError.code ?? "model_quality_score.update_failed")}</strong>
        <span>${escapeHtml(row.actionError.message ?? "模型评分保存失败")}</span>
        ${row.actionError.suggestion ? `<span>${escapeHtml(row.actionError.suggestion)}</span>` : ""}
        <code>request_id: ${escapeHtml(row.actionError.requestId ?? row.actionError.request_id ?? "-")}</code>
      </div>
    `;
  }
  return row.feedback
    ? `<div class="model-quality-row-feedback" role="status" aria-live="polite" tabindex="-1">${escapeHtml(row.feedback)}</div>`
    : "";
}

/** 后台刷新失败时保留已加载统计和人工草稿，只显示可追踪告警。 */
function renderRefreshError(error) {
  return error
    ? `<div class="model-quality-refresh-error" role="status">后台刷新失败，当前草稿已保留。request_id: ${escapeHtml(error.requestId ?? "-")}</div>`
    : "";
}

/** 首次读取前渲染稳定占位，不伪造评分或版本。 */
function renderLoading() {
  return `
    <div class="model-quality-heading"><div><h2 id="model-quality-scores-title">模型评分</h2><p>正在读取当前租户模型统计。</p></div></div>
    <div class="empty-state">等待 /model-quality-scores 响应。</div>
  `;
}

/** 首次读取失败只展示统一错误和 request_id。 */
function renderFatalError(error) {
  return `
    <div class="model-quality-heading"><div><h2 id="model-quality-scores-title">模型评分</h2><p>模型评分暂不可用。</p></div></div>
    <div class="empty-state error-state" role="alert">${escapeHtml(error?.message ?? "模型评分读取失败")} request_id: ${escapeHtml(error?.requestId ?? "-")}</div>
  `;
}

/** 当前分页没有模型时渲染单轨空状态。 */
function renderEmptyRow() {
  return `<div class="model-quality-row model-quality-empty" role="row"><span>当前筛选没有模型评分。</span></div>`;
}

/** 渲染分页按钮；offset 由主应用维护，按钮本身不推导租户范围。 */
function renderPager(state) {
  const offset = nonnegativeInteger(state.offset);
  const limit = positiveInteger(state.limit, 100);
  const total = nonnegativeInteger(state.total);
  return `
    <div class="model-quality-pager">
      <button type="button" id="model-quality-prev" ${offset <= 0 ? "disabled" : ""}>上一页</button>
      <span>${escapeHtml(total ? `${offset + 1}-${Math.min(offset + limit, total)} / ${total}` : "0 / 0")}</span>
      <button type="button" id="model-quality-next" ${offset + limit >= total ? "disabled" : ""}>下一页</button>
    </div>
  `;
}

/** 将 bps 转为百分比展示，整数拆分避免浏览器浮点舍入漂移。 */
function formatBps(value) {
  const bps = validBps(value);
  if (bps === null) {
    return "-";
  }
  const whole = Math.floor(bps / 100);
  const fraction = String(bps % 100).padStart(2, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}%`;
}

/** 人工评分为空时明确显示未设置，0 分仍作为合法事实展示。 */
function formatManualQuality(value) {
  return value === null || value === undefined ? "未设置" : formatBps(value);
}

/** 把 bps 转为 number input 文本；null 清除态使用空值。 */
function bpsInputValue(value) {
  const bps = validOptionalBps(value);
  if (bps === null || bps === undefined) {
    return "";
  }
  const whole = Math.floor(bps / 100);
  const fraction = String(bps % 100).padStart(2, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}`;
}

/** 平均延迟保持后端 Decimal 文本精度，页面只追加单位不重算。 */
function formatLatency(value) {
  return value === null || value === undefined || value === "" ? "-" : `${value} ms`;
}

/** 将后端整数 milli 格式化为最多三位小数，不使用浮点重新计算权威平均分。 */
function formatRatingMilli(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    return "-";
  }
  const whole = Math.floor(value / 1000);
  const fraction = String(value % 1000).padStart(3, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""} / 5`;
}

/** 将后端五档分布压缩为可扫描文本；缺失或非法计数失败关闭为 0，不参与任何质量公式。 */
function formatRequestFeedbackDistribution(value) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return [1, 2, 3, 4, 5]
    .map((rating) => `${rating}:${nonnegativeInteger(source[String(rating)])}`)
    .join(" / ");
}

/** 将后端 observation 状态转换为中文，未知枚举保留原值便于排障。 */
function observationLabel(value) {
  return { observed: "已有观测", unobserved: "暂无观测" }[value] ?? value ?? "-";
}

/** 校验严格 bps 整数；bool、字符串、浮点和越界值都不得进入评分草稿。 */
function validBps(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= 10000
    ? value
    : null;
}

/** 校验可空 bps；undefined 代表损坏输入，null 代表合法清除意图。 */
function validOptionalBps(value) {
  if (value === null) {
    return null;
  }
  return validBps(value) ?? undefined;
}

/** feedback/statistics version 必须是 JavaScript 可精确表达的正整数。 */
function validVersion(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : null;
}

/** 将分页值收敛为非负安全整数，异常服务端值不参与按钮运算。 */
function nonnegativeInteger(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

/** 将分页大小收敛为正整数，异常服务端值使用稳定默认值。 */
function positiveInteger(value, fallback) {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

/** 复制公开 Score DTO，避免权威值和旁路快照共享可变对象引用。 */
function copyScore(score) {
  return score && typeof score === "object" && !Array.isArray(score) ? { ...score } : {};
}

/** 把可空 DTO 字段收敛为字符串，后续统一执行 HTML 转义。 */
function text(value) {
  return value == null ? "" : String(value);
}

/** HTML 文本转义，防止模型名、Provider 名和错误字段破坏页面结构。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** HTML 属性转义额外处理反引号，保护 data-* 和 value 属性。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
