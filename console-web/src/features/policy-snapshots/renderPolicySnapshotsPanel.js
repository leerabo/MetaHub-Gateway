import { createPolicySnapshotRequestId } from "../../lib/policySnapshots.js";
import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

/**
 * 模块职责：渲染获 ``policy.publish`` 授权的策略发布与不可变快照历史，并提供页面生命周期和幂等操作内存。
 * 安全边界：组件只展示公共摘要；DSL、AST、Prompt、Response、tenant_id 和任何密钥都不进入 DOM。
 */

// 写操作期间锁定所有发布/回滚命令，避免同一页面并发形成多个不确定事务。
const BUSY_OPERATION_STATES = new Set(["publishing", "rolling_back"]);

/**
 * 渲染策略快照发布面板。
 *
 * @param {object|null} state 当前、历史、CAS 和页面交互状态。
 * @param {object|null} authContext 后端认证上下文；前端权限快照只用于避免无意义预取和 DOM 暴露。
 * @returns {string} 已完成转义的 HTML；缺少发布权限时返回空字符串。
 */
export function renderPolicySnapshotsPanel(state, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.POLICY_PUBLISH)) {
    // 真实权限仍由后端 ``require_policy_publisher`` 执行；这里不为未授权身份生成隐藏表单或原因正文。
    return "";
  }
  if (!state) {
    return renderLoadingState();
  }
  // current/history 任一读取失败都不能沿用旧 CAS 或伪造版本，页面以统一错误失败关闭。
  if (state.state === "error") {
    return renderFatalError(state);
  }

  const current = state.current_snapshot ?? null;
  const history = Array.isArray(state.policy_snapshots) ? state.policy_snapshots : [];
  return `
    ${renderHeading(state, current)}
    ${renderActionFeedback(state)}
    ${renderPendingOperationNotice(state)}
    ${renderCurrentSnapshot(current)}
    ${renderCandidateSummary(state, current)}
    ${renderPublishForm(state)}
    ${renderHistory(state, history, current)}
  `;
}

/**
 * 判断某一轮刷新是否仍有资格读取发布快照正文。
 *
 * hash、active navigation、刷新代次和身份必须同时匹配，避免旧 tenant 或旧角色响应在离页后覆盖新页面。
 */
export function shouldLoadPolicySnapshots({
  canPublish,
  activeNavigationItemId,
  hashNavigation,
  refreshId,
  currentRefreshId,
  requestIdentity,
  currentIdentity,
}) {
  return canPublish === true
    && activeNavigationItemId === "policy-snapshots"
    && (hashNavigation === undefined || hashNavigation === "policy-snapshots")
    && refreshId === currentRefreshId
    && Boolean(requestIdentity)
    && requestIdentity === currentIdentity;
}

/**
 * 从认证上下文提取策略快照页面身份。
 * tenant、user、role 和 session 任一变化都必须视为新边界，不能复用原因、ETag 或未决幂等键。
 */
export function policySnapshotIdentityFromAuthContext(context) {
  return {
    tenantId: context?.user?.tenant_id ?? context?.tenant?.tenant_id ?? "",
    userId: context?.user?.user_id ?? context?.user?.email ?? "",
    role: context?.user?.role ?? "",
    sessionId: context?.session?.session_id ?? "",
  };
}

/**
 * 把身份对象转换为不可歧义的比较快照。
 * JSON 数组固定字段顺序，避免字符串分隔符出现在租户或邮箱中时造成碰撞。
 */
export function policySnapshotIdentitySnapshot(identity) {
  if (!identity?.tenantId || !identity?.userId || !identity?.role) {
    return "";
  }
  return JSON.stringify([
    identity.tenantId,
    identity.userId,
    identity.role,
    identity.sessionId ?? "",
  ]);
}

/**
 * 创建 active-only 读取生命周期。
 *
 * 新读取会取消前一读取；即使测试替身或底层 fetch 忽略 AbortSignal，isCurrent 仍会按 owner 和身份丢弃旧响应。
 */
export function createPolicySnapshotLoadLifecycle() {
  let activeTicket = null;
  let sequence = 0;
  return {
    /** 开始一轮读取并返回唯一 owner。 */
    begin(owner) {
      activeTicket?.controller.abort();
      const controller = new AbortController();
      activeTicket = {
        ...owner,
        sequence: ++sequence,
        controller,
        signal: controller.signal,
      };
      return activeTicket;
    },
    /** 核对回调仍属于当前 owner、刷新代次、导航与身份。 */
    isCurrent(ticket, owner) {
      return activeTicket === ticket
        && !ticket.signal.aborted
        && ticket.refreshId === owner.refreshId
        && ticket.navigation === owner.navigation
        && ticket.identity === owner.identity;
    },
    /** 完成当前 owner；旧 owner 调用 complete 不得清掉新请求。 */
    complete(ticket) {
      if (activeTicket === ticket) {
        activeTicket = null;
      }
    },
    /** 离页、登出或身份切换时取消读取并使所有旧回调失效。 */
    invalidate() {
      activeTicket?.controller.abort();
      activeTicket = null;
      sequence += 1;
    },
    /** 返回是否存在活动读取，供交互测试和页面调度使用。 */
    isBusy() {
      return activeTicket !== null;
    },
  };
}

/**
 * 创建未决发布/回滚操作的纯内存幂等管理器。
 *
 * 网络结果未知时同一操作复用 request_id 和原 CAS；若用户试图在未决结果上改变原因、目标或 CAS，
 * 管理器返回 blocked，避免“原操作可能已成功”时再制造第二个版本。
 */
export function createPolicySnapshotOperationMemory({ requestIdFactory = createPolicySnapshotRequestId } = {}) {
  let activeIdentity = "";
  let pending = null;
  return {
    /** 进入一个身份；身份变化会立即销毁上一租户的未决键与原因指纹。 */
    enter(identity) {
      if (activeIdentity !== identity) {
        activeIdentity = identity;
        pending = null;
      }
      return pending;
    },
    /** 获取操作票据；完全相同的未知结果重试复用原票据，其余操作在明确清理前被阻止。 */
    acquire(identity, descriptor) {
      this.enter(identity);
      const normalized = normalizeOperationDescriptor(descriptor);
      const fingerprint = JSON.stringify(normalized);
      if (pending && pending.fingerprint !== fingerprint) {
        return { state: "blocked", pending: { ...pending } };
      }
      if (pending) {
        return { state: "ok", operation: { ...pending }, reused: true };
      }
      pending = {
        ...normalized,
        fingerprint,
        requestId: requestIdFactory(),
      };
      return { state: "ok", operation: { ...pending }, reused: false };
    },
    /** 网络异常不清除票据；显式方法使调用处能清楚表达“结果未知”语义。 */
    markUncertain(operation) {
      return Boolean(pending && operation?.requestId === pending.requestId);
    },
    /** 服务端成功或明确错误均是确定结果，仅当前票据可以清除自身。 */
    markDefinitive(operation) {
      if (pending && operation?.requestId === pending.requestId) {
        pending = null;
        return true;
      }
      return false;
    },
    /** 读取安全副本，渲染层只使用 kind 和 requestId 判断重试提示。 */
    read(identity) {
      this.enter(identity);
      return pending ? { ...pending } : null;
    },
    /** 离页、登出或用户明确放弃未知结果后清理内存，不写 localStorage/sessionStorage。 */
    clear() {
      pending = null;
      activeIdentity = "";
    },
  };
}

/**
 * 校验变更原因的前端可提交形态。
 * 后端仍会重新执行 Unicode、长度和控制字符校验；前端校验用于即时反馈和按钮状态。
 */
export function isPolicySnapshotReasonValid(value) {
  const normalized = String(value ?? "").trim();
  return normalized.length >= 1
    && normalized.length <= 256
    && !/[\u0000-\u001f\u007f-\u009f]/.test(normalized);
}

/**
 * 规范化幂等指纹字段，固定顺序并丢弃调用方多余属性。
 * reason 只留在页面内存，不进入日志、URL 或持久存储。
 */
function normalizeOperationDescriptor(descriptor) {
  return {
    kind: descriptor?.kind === "rollback" ? "rollback" : "publish",
    targetId: String(descriptor?.targetId ?? ""),
    reason: String(descriptor?.reason ?? "").trim(),
    headEtag: String(descriptor?.headEtag ?? ""),
    candidateEtag: String(descriptor?.candidateEtag ?? ""),
  };
}

/** 渲染接口返回前的稳定加载态，不伪造当前版本或候选状态。 */
function renderLoadingState() {
  return `
    <div class="policy-snapshot-heading">
      <div>
        <h2 id="policy-snapshots-title">策略发布</h2>
        <p>正在读取当前线上版本和不可变历史。</p>
      </div>
    </div>
    <div class="policy-snapshot-empty">等待 /policy-snapshots 响应。</div>
  `;
}

/** 渲染致命读取错误，只显示统一错误、request_id 和安全建议。 */
function renderFatalError(state) {
  return `
    <div class="policy-snapshot-heading">
      <div>
        <h2 id="policy-snapshots-title">策略发布</h2>
        <p>当前版本和历史暂不可用。</p>
      </div>
    </div>
    ${renderError(state)}
  `;
}

/** 渲染页头和历史总数；数量来自当前租户后端分页响应。 */
function renderHeading(state, current) {
  const versionLabel = current ? `当前 v${current.snapshot_version}` : "尚未发布";
  return `
    <div class="policy-snapshot-heading">
      <div>
        <h2 id="policy-snapshots-title">策略发布</h2>
        <p>显式发布 active 策略候选，查看不可变版本历史，并只回滚上一激活版本。</p>
      </div>
      <div class="policy-snapshot-heading-meta">
        <strong>${escapeHtml(versionLabel)}</strong>
        <span>${escapeHtml(state.total ?? 0)} 个历史版本</span>
      </div>
    </div>
  `;
}

/** 渲染当前快照摘要；完整 hash 只在 code 节点展示，不进入 URL。 */
function renderCurrentSnapshot(current) {
  if (!current) {
    return `
      <section class="policy-snapshot-current" aria-labelledby="policy-snapshot-current-title">
        <div>
          <h3 id="policy-snapshot-current-title">当前线上版本</h3>
          <strong>尚未发布</strong>
        </div>
        <p>数据面在首次发布前使用“未发布基线”，不会把空值伪装成 v0。</p>
      </section>
    `;
  }
  const operation = operationLabel(current);
  return `
    <section class="policy-snapshot-current" aria-labelledby="policy-snapshot-current-title">
      <div class="policy-snapshot-current-title">
        <div>
          <h3 id="policy-snapshot-current-title">当前线上版本</h3>
          <strong>v${escapeHtml(current.snapshot_version)}</strong>
        </div>
        <span class="snapshot-operation snapshot-operation-${escapeAttribute(current.operation)}">${escapeHtml(operation)}</span>
      </div>
      <dl class="policy-snapshot-summary-grid">
        ${renderPair("策略数量", current.policy_count)}
        ${renderPair("审批状态", approvalLabel(current.approval_status))}
        ${renderPair("发布人", actorLabel(current.created_by))}
        ${renderPair("发布时间", formatDateTime(current.published_at))}
        ${renderPair("变更原因", current.change_reason)}
        ${renderPair("版本来源", versionSourceLabel(current))}
      </dl>
      <div class="policy-snapshot-hash-line">
        <span>内容 hash</span>
        <code>${escapeHtml(current.content_hash ?? "-")}</code>
      </div>
    </section>
  `;
}

/**
 * 渲染 active 候选与线上 head 的漂移状态。
 * 回滚不会修改 active Policy，因此回滚后漂移文案必须明确下一次 publish 会恢复候选内容。
 */
function renderCandidateSummary(state, current) {
  const invalid = state.candidate_status !== "valid";
  const matches = state.candidate_matches_current === true;
  let title = "候选可发布";
  let message = `当前 active 候选包含 ${state.candidate_policy_count ?? 0} 条策略，发布后数据面将在轮询周期内拉取。`;
  let tone = "changed";
  if (invalid) {
    title = "候选无效";
    message = "active 策略候选无法安全编译。请先回到策略管理修正，页面不会暴露 DSL 或内部诊断正文。";
    tone = "invalid";
  } else if (matches) {
    title = "候选与线上一致";
    message = "当前 active 候选与线上内容 hash 一致，无需制造新版本。";
    tone = "matched";
  } else if (current?.operation === "rollback") {
    title = "回滚内容与候选不同";
    message = "回滚只改变线上快照，不修改 active Policy。下一次发布会重新上线当前候选内容。";
    tone = "warning";
  } else if (!current) {
    title = "等待首次发布";
    message = `已编译 ${state.candidate_policy_count ?? 0} 条 active 策略；首次发布也允许建立明确的空策略基线。`;
  }
  return `
    <section class="policy-snapshot-candidate candidate-${escapeAttribute(tone)}" aria-labelledby="policy-snapshot-candidate-title">
      <div>
        <h3 id="policy-snapshot-candidate-title">${escapeHtml(title)}</h3>
        <p>${escapeHtml(message)}</p>
      </div>
      <div class="policy-snapshot-candidate-meta">
        <span>${escapeHtml(state.candidate_policy_count ?? "-")} 条策略</span>
        <code>${escapeHtml(shortHash(state.candidate_content_hash))}</code>
      </div>
    </section>
  `;
}

/** 渲染发布原因表单；按钮状态只是体验约束，后端仍执行 admin、双 CAS 和完整候选重编译。 */
function renderPublishForm(state) {
  const busy = BUSY_OPERATION_STATES.has(state.operationStatus);
  const pending = state.pendingOperation?.kind === "publish";
  const reason = pending ? state.pendingOperation.reason : state.publishReason ?? "";
  const pendingOtherOperation = Boolean(state.pendingOperation && !pending);
  const candidateReady = (state.candidate_status === "valid"
    && Boolean(state.headEtag)
    && Boolean(state.candidateEtag)
    && state.candidate_matches_current !== true) || pending;
  const disabled = busy || pendingOtherOperation || !candidateReady || !isPolicySnapshotReasonValid(reason);
  return `
    <section class="policy-snapshot-publish" aria-labelledby="policy-snapshot-publish-title">
      <div class="policy-snapshot-section-heading">
        <div>
          <h3 id="policy-snapshot-publish-title">发布候选</h3>
          <p>发布成功表示版本已提交；数据面将在下一轮轮询时拉取，不代表所有实例已 ACK。</p>
        </div>
        ${pending ? `<span class="policy-snapshot-pending">结果待确认，重试将复用 ${escapeHtml(state.pendingOperation.requestId)}</span>` : ""}
      </div>
      <form id="policy-snapshot-publish-form" class="policy-snapshot-reason-form">
        <label>
          <span>变更原因</span>
          <textarea name="reason" rows="3" maxlength="256" placeholder="说明本次上线目的、验证结论或变更单号" ${busy ? "disabled" : pending ? "readonly" : ""}>${escapeHtml(reason)}</textarea>
        </label>
        <div class="policy-snapshot-form-actions">
          <button type="submit" data-policy-snapshot-publish ${disabled ? "disabled" : ""}>${busy ? "发布中" : pending ? "重试发布" : "发布候选"}</button>
        </div>
      </form>
    </section>
  `;
}

/** 渲染历史表格和分页；桌面是表格，中窄屏由 data-label 转为逐行键值布局。 */
function renderHistory(state, history, current) {
  return `
    <section class="policy-snapshot-history" aria-labelledby="policy-snapshot-history-title">
      <div class="policy-snapshot-section-heading">
        <div>
          <h3 id="policy-snapshot-history-title">快照历史</h3>
          <p>版本严格递增；回滚会复制旧内容并创建新的更高版本。</p>
        </div>
      </div>
      <div class="policy-snapshot-table" role="table" aria-label="当前租户策略快照历史">
        <div class="policy-snapshot-row policy-snapshot-row-head" role="row">
          <span>版本</span>
          <span>来源</span>
          <span>策略 / hash</span>
          <span>原因 / 操作者</span>
          <span>发布时间</span>
          <span>操作</span>
        </div>
        ${history.length ? history.map((item) => renderHistoryRow(item, state, current)).join("") : renderEmptyHistory()}
      </div>
      ${renderHistoryPager(state)}
    </section>
  `;
}

/** 渲染单个历史版本；只有 current.previous_snapshot_version 对应行可以展开回滚。 */
function renderHistoryRow(item, state, current) {
  const isCurrent = item.policy_snapshot_id === current?.policy_snapshot_id;
  const isPrevious = item.snapshot_version === current?.previous_snapshot_version;
  const rollbackOpen = state.rollbackTargetId === item.policy_snapshot_id;
  const busy = BUSY_OPERATION_STATES.has(state.operationStatus);
  const pendingOtherOperation = Boolean(
    state.pendingOperation
      && (state.pendingOperation.kind !== "rollback" || state.pendingOperation.targetId !== item.policy_snapshot_id),
  );
  return `
    <div class="policy-snapshot-row${isCurrent ? " is-current" : ""}" role="row">
      <span data-label="版本" class="policy-snapshot-version">
        <strong>v${escapeHtml(item.snapshot_version)}</strong>
        ${isCurrent ? `<mark>当前</mark>` : ""}
      </span>
      <span data-label="来源">
        <strong>${escapeHtml(operationLabel(item))}</strong>
        <em>${escapeHtml(versionSourceLabel(item))}</em>
      </span>
      <span data-label="策略 / hash" class="policy-snapshot-row-hash">
        <strong>${escapeHtml(item.policy_count)} 条策略</strong>
        <code title="${escapeAttribute(item.content_hash ?? "")}">${escapeHtml(shortHash(item.content_hash))}</code>
      </span>
      <span data-label="原因 / 操作者" class="policy-snapshot-row-reason">
        <strong>${escapeHtml(item.change_reason ?? "-")}</strong>
        <em>${escapeHtml(actorLabel(item.created_by))}</em>
      </span>
      <time data-label="发布时间" datetime="${escapeAttribute(item.published_at ?? "")}">${escapeHtml(formatDateTime(item.published_at))}</time>
      <span data-label="操作" class="policy-snapshot-row-action">
        ${isPrevious ? `<button type="button" data-policy-snapshot-rollback-open="${escapeAttribute(item.policy_snapshot_id)}" ${busy || pendingOtherOperation ? "disabled" : ""}>回滚此版</button>` : "-"}
      </span>
    </div>
    ${isPrevious && rollbackOpen ? renderRollbackForm(item, state) : ""}
  `;
}

/** 渲染上一版行内回滚表单，明确回滚不改 active 候选。 */
function renderRollbackForm(item, state) {
  const busy = BUSY_OPERATION_STATES.has(state.operationStatus);
  const pending = state.pendingOperation?.kind === "rollback";
  const reason = pending ? state.pendingOperation.reason : state.rollbackReason ?? "";
  const pendingOtherOperation = Boolean(state.pendingOperation && !pending);
  const disabled = busy
    || pendingOtherOperation
    || (!state.headEtag && !pending)
    || !isPolicySnapshotReasonValid(reason);
  return `
    <form class="policy-snapshot-rollback-form" data-policy-snapshot-rollback-form="${escapeAttribute(item.policy_snapshot_id)}">
      <div>
        <strong>回滚到 v${escapeHtml(item.snapshot_version)} 的内容</strong>
        <p>将创建新的单调版本；active Policy 候选保持不变，下一次发布可能再次恢复候选内容。</p>
      </div>
      <label>
        <span>回滚原因</span>
        <textarea name="reason" rows="2" maxlength="256" placeholder="说明故障现象、影响和回滚依据" ${busy ? "disabled" : pending ? "readonly" : ""}>${escapeHtml(reason)}</textarea>
      </label>
      <div class="policy-snapshot-form-actions">
        <button type="submit" ${disabled ? "disabled" : ""}>${busy ? "回滚中" : pending ? "重试回滚" : "确认回滚"}</button>
        <button type="button" data-policy-snapshot-rollback-cancel ${busy ? "disabled" : ""}>取消</button>
      </div>
    </form>
  `;
}

/** 渲染无历史态；明确“尚未发布”而不是生成虚构 v0。 */
function renderEmptyHistory() {
  return `
    <div class="policy-snapshot-row policy-snapshot-empty" role="row">
      <span>尚未发布任何策略快照。</span>
    </div>
  `;
}

/** 渲染历史分页，页码变化只重新读取当前租户，不能把 offset 放进快照写请求。 */
function renderHistoryPager(state) {
  const limit = Number(state.limit ?? 20);
  const offset = Number(state.offset ?? 0);
  const total = Number(state.total ?? 0);
  const canPrevious = offset > 0;
  const canNext = offset + limit < total;
  const first = total ? offset + 1 : 0;
  const last = Math.min(offset + limit, total);
  return `
    <div class="policy-snapshot-pager" data-limit="${escapeAttribute(limit)}" data-offset="${escapeAttribute(offset)}">
      <button type="button" data-policy-snapshot-page="previous" ${canPrevious ? "" : "disabled"}>上一页</button>
      <span>${escapeHtml(first)}-${escapeHtml(last)} / ${escapeHtml(total)}</span>
      <button type="button" data-policy-snapshot-page="next" ${canNext ? "" : "disabled"}>下一页</button>
    </div>
  `;
}

/** 渲染非致命操作错误和成功反馈；两者都保留 request_id 便于排障。 */
function renderActionFeedback(state) {
  const error = state.actionError ? renderError(state.actionError) : "";
  const feedback = state.feedback
    ? `<div class="policy-snapshot-feedback" role="status">${escapeHtml(state.feedback)}</div>`
    : "";
  return `${error}${feedback}`;
}

/**
 * 渲染全局未决操作控制。
 * 即使提交实际成功后 target 已不再是“上一版”或历史翻页隐藏了表单，管理员仍能放弃原票据并权威刷新。
 */
function renderPendingOperationNotice(state) {
  const pending = state.pendingOperation;
  if (!pending) {
    return "";
  }
  const operationLabel = pending.kind === "rollback" ? "回滚" : "发布";
  return `
    <div class="policy-snapshot-pending-notice" role="status">
      <div>
        <strong>${escapeHtml(operationLabel)}结果待确认</strong>
        <span>安全重试将复用原原因、CAS 和 request_id：${escapeHtml(pending.requestId)}</span>
      </div>
      <button type="button" data-policy-snapshot-abandon>放弃未决重试并刷新</button>
    </div>
  `;
}

/** 渲染统一错误，不反射 reason、ETag、DSL 或服务端内部异常正文。 */
function renderError(error) {
  return `
    <div class="policy-snapshot-error" role="alert">
      <strong>${escapeHtml(error.code ?? "policy_snapshot.unavailable")}</strong>
      <span>${escapeHtml(error.message ?? "策略快照操作失败")}</span>
      ${error.suggestion ? `<span>${escapeHtml(error.suggestion)}</span>` : ""}
      <code>request_id: ${escapeHtml(error.requestId ?? error.request_id ?? "-")}</code>
    </div>
  `;
}

/** 渲染摘要键值对，所有服务端文本都经统一转义。 */
function renderPair(label, value) {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "-")}</dd></div>`;
}

/** 把 publish/rollback 与来源版本转换为可解释中文。 */
function operationLabel(item) {
  if (item?.operation === "rollback") {
    return `回滚 v${item.rollback_source_snapshot_version ?? "-"}`;
  }
  return "发布";
}

/** 描述当前版本与前序/回滚来源的关系，避免把回滚误解为版本号倒退。 */
function versionSourceLabel(item) {
  if (item?.operation === "rollback") {
    return `v${item.snapshot_version} <- 回滚 v${item.rollback_source_snapshot_version ?? "-"}`;
  }
  return item?.previous_snapshot_version
    ? `接续 v${item.previous_snapshot_version}`
    : "首次发布";
}

/** 组合操作者安全标识；公共 DTO 不含密码、Token 或租户外信息。 */
function actorLabel(actor) {
  return actor?.display_name || actor?.email || actor?.user_id || "-";
}

/** 映射 M1 审批状态，避免内部枚举直接主导界面文案。 */
function approvalLabel(value) {
  return value === "not_required" ? "无需审批" : value ?? "-";
}

/** 缩短内容 hash 用于表格扫描；完整值仍保留在安全 title 或当前 code 节点。 */
function shortHash(value) {
  const normalized = String(value ?? "");
  return normalized ? `${normalized.slice(0, 12)}${normalized.length > 12 ? "..." : ""}` : "-";
}

/** 格式化 UTC ISO 时间，不依赖浏览器本地时区推导版本先后。 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/** HTML 文本转义，保护 change_reason、actor、ID、hash 和统一错误字段。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** HTML 属性转义，额外处理反引号以保护 data-*、title 和 datetime 属性。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
