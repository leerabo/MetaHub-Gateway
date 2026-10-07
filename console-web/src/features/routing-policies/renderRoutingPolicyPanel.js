/**
 * 模块职责：渲染 PAGE-072 路由策略页，并维护按身份、导航和别名隔离的行级草稿生命周期。
 * 安全边界：候选只来自 ModelAlias 主备摘要；前端不接收 tenant_id 输入，也不以隐藏按钮替代后端 RBAC。
 */

import { createRoutingPolicyRequestId } from "../../lib/modelAliases.js";
import {
  authorizationIdentitySnapshot,
  hasPermission,
  PERMISSIONS,
} from "../../lib/permissions.js";
import { renderCacheAffinityRow } from "./cacheAffinityControls.js";

// 四种模式是 PATCH 的唯一合法值；未知服务端值只展示，绝不能被表单再次提交。
export const ROUTING_MODES = Object.freeze(["fixed", "cost_first", "availability_first", "quality_cost"]);
export const ROUTING_MODE_LABELS = Object.freeze({
  fixed: "固定",
  cost_first: "成本优先",
  availability_first: "可用性优先",
  quality_cost: "质量、成本与延迟",
});
export const ROUTING_MODE_DESCRIPTIONS = Object.freeze({
  fixed: "请求开始时按你设置的顺序选择；请求出错后，可按故障切换尝试下一候选。",
  cost_first: "请求开始时按已配置价格自动选择；请求出错后，可按故障切换尝试下一候选。",
  availability_first: "请求开始时优先选择当前可用模型；请求出错后，可按故障切换尝试下一候选。",
  quality_cost: "请求开始时综合质量、成本和响应速度自动选择；请求出错后，可按故障切换尝试下一候选。",
});
// 休眠默认配置与后端迁移一致；旧模式不发送这些字段，重新启用 quality_cost 时必须显式确认。
const DEFAULT_QUALITY_COST_CONFIG = Object.freeze({
  quality_weight_bps: 5000,
  cost_weight_bps: 3000,
  latency_weight_bps: 2000,
  quality_floor_bps: 6000,
  minimum_sample_count: 20,
});
// 指纹、复制与 wire 白名单只遍历这五个固定字段，未知属性不得参与请求意图。
const QUALITY_COST_CONFIG_FIELDS = Object.freeze(Object.keys(DEFAULT_QUALITY_COST_CONFIG));
/**
 * 从认证上下文提取不可歧义的路由策略页面身份。
 * tenant、user、role、Session ID 或 Session revision 任一变化都会使旧保存响应失效。
 */
export function routingPolicyIdentitySnapshot(authContext) {
  // owner 绑定后端路由读权限及矩阵 revision/fingerprint，降权后旧草稿立即失效。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.ROUTING_READ);
}

/**
 * 创建 PAGE-067 的纯内存状态仓库。
 *
 * refresh_generation 只淘汰旧 GET；operation_generation 只由保存、明确取消或身份/页面失效推进。
 * 这两个代次刻意存放在每一行，防止后台刷新误判一个合法 PATCH 200 已经过期。
 */
export function createRoutingPolicyPageMemory({ requestIdFactory } = {}) {
  const createRequestId = requestIdFactory ?? createRoutingPolicyRequestId;
  const rows = new Map();
  let activeIdentity = "";
  let activeNavigation = "";
  let refreshSequence = 0;
  let loadError = null;

  /** 切换身份或导航时先销毁旧行；相同 owner 的定时刷新不会触碰 operation generation。 */
  function enter(identity, navigation) {
    const nextIdentity = text(identity);
    const nextNavigation = text(navigation);
    if (activeIdentity === nextIdentity && activeNavigation === nextNavigation) {
      return;
    }
    invalidateRows();
    activeIdentity = nextIdentity;
    activeNavigation = nextNavigation;
  }

  /** 取消所有活动保存并推进失效代次，确保忽略 fetch 不响应 Abort 的迟到回调。 */
  function invalidateRows() {
    for (const row of rows.values()) {
      row.ticket?.controller.abort();
      row.ticket = null;
      row.operationGeneration += 1;
    }
    rows.clear();
    refreshSequence += 1;
    loadError = null;
  }

  /** 把服务器对象应用为干净权威值；调用方保证当前行没有需要保留的草稿。 */
  function adoptAuthority(row, alias) {
    const authoritative = copyAlias(alias);
    const policy = policyFacts(authoritative);
    row.authoritative = authoritative;
    row.basePolicyVersion = policy.version;
    row.draftMode = policy.mode;
    row.draftQualityCostConfig = copyQualityCostConfig(policy.qualityCostConfig);
    row.draftRevision += 1;
    row.dirty = false;
    row.conflicted = false;
    row.sideAuthoritativeSnapshot = null;
    row.sideSnapshotMissing = false;
    row.retryOperation = null;
  }

  /** 为第一次出现的 Alias 建立行状态；版本为空时保持只读失败关闭，不伪造 v1。 */
  function createRow(alias, refreshGeneration) {
    const authoritative = copyAlias(alias);
    const policy = policyFacts(authoritative);
    return {
      modelAliasId: text(authoritative.model_alias_id),
      authoritative,
      basePolicyVersion: policy.version,
      draftMode: policy.mode,
      draftQualityCostConfig: copyQualityCostConfig(policy.qualityCostConfig),
      draftRevision: 0,
      dirty: false,
      conflicted: false,
      sideAuthoritativeSnapshot: null,
      sideSnapshotMissing: false,
      refreshGeneration,
      operationGeneration: 0,
      ticket: null,
      retryOperation: null,
      actionError: null,
      feedback: "",
    };
  }

  /** 核对 GET ticket 仍属于当前身份、页面和最新 refresh generation。 */
  function isCurrentRefresh(ticket) {
    return Boolean(
      ticket
      && ticket.generation === refreshSequence
      && ticket.identity === activeIdentity
      && ticket.navigation === activeNavigation,
    );
  }

  /** 核对 PATCH ticket 仍属于当前行；refresh generation 和旁路版本故意不参与判断。 */
  function isCurrentOperation(ticket, owner = {}) {
    if (!ticket || ticket.identity !== activeIdentity || ticket.navigation !== activeNavigation) {
      return false;
    }
    const row = rows.get(ticket.modelAliasId);
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
    /** 开始一轮 ModelAlias GET；不会取消或推进任何保存操作。 */
    beginRefresh({ identity, navigation }) {
      enter(identity, navigation);
      const generation = ++refreshSequence;
      for (const row of rows.values()) {
        row.refreshGeneration = generation;
      }
      return Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        generation,
      });
    },

    /**
     * 接纳最新 GET。
     * clean 行直接采用；dirty/busy/conflict 行只写旁路权威快照，不改变 base version、ticket 或 operation generation。
     */
    acceptRefresh(ticket, aliases) {
      if (!isCurrentRefresh(ticket)) {
        return false;
      }
      const seen = new Set();
      for (const alias of Array.isArray(aliases) ? aliases : []) {
        const modelAliasId = text(alias?.model_alias_id);
        if (!modelAliasId) {
          continue;
        }
        seen.add(modelAliasId);
        const existing = rows.get(modelAliasId);
        if (!existing) {
          rows.set(modelAliasId, createRow(alias, ticket.generation));
          continue;
        }
        existing.refreshGeneration = ticket.generation;
        if (existing.dirty || existing.ticket || existing.conflicted) {
          existing.sideAuthoritativeSnapshot = copyAlias(alias);
          existing.sideSnapshotMissing = false;
        } else {
          adoptAuthority(existing, alias);
        }
      }
      // 并发删除不能悄悄移除正在编辑的行；该行会在保存时得到租户内 404，并保留 request_id。
      for (const [modelAliasId, row] of rows) {
        if (seen.has(modelAliasId)) {
          continue;
        }
        if (row.dirty || row.ticket || row.conflicted) {
          row.sideAuthoritativeSnapshot = null;
          row.sideSnapshotMissing = true;
        } else {
          rows.delete(modelAliasId);
        }
      }
      loadError = null;
      return true;
    },

    /** 记录最新 GET 的统一错误；有草稿时保留行内容，初次加载失败则渲染致命错误态。 */
    acceptRefreshError(ticket, error) {
      if (!isCurrentRefresh(ticket)) {
        return false;
      }
      loadError = error;
      return true;
    },

    /** 修改某行草稿；切换控件不会立即改变 authoritative mode 或线上流量。 */
    selectMode(modelAliasId, mode) {
      const row = rows.get(text(modelAliasId));
      if (!row || row.ticket || row.conflicted || !ROUTING_MODES.includes(mode)) {
        return false;
      }
      row.draftMode = mode;
      row.draftRevision += 1;
      row.dirty = routingPolicyDraftIsDirty(row);
      row.actionError = null;
      row.feedback = row.dirty ? "存在未保存选择" : "";
      if (!row.dirty) {
        row.retryOperation = null;
      }
      return true;
    },

    /**
     * 更新一行完整质量成本草稿。
     * null 字段代表浏览器输入尚不合法，仍保留在内存中以关闭保存；只有五字段完整且权重和为 10000 才可建 ticket。
     */
    selectQualityCostConfig(modelAliasId, config) {
      const row = rows.get(text(modelAliasId));
      if (!row || row.ticket || row.conflicted) {
        return false;
      }
      row.draftQualityCostConfig = normalizeQualityCostConfig(config, { useDefaults: false });
      row.draftRevision += 1;
      row.dirty = routingPolicyDraftIsDirty(row);
      row.actionError = null;
      row.feedback = row.dirty ? "存在未保存质量成本配置" : "";
      if (!row.dirty) {
        row.retryOperation = null;
      }
      return true;
    },

    /**
     * 获取唯一保存 ticket。
     * 重复点击返回 reused=true，不产生第二个 PATCH；网络结果未知时同一意图复用原 request_id。
     */
    acquireSave({ identity, navigation, modelAliasId }) {
      enter(identity, navigation);
      const row = rows.get(text(modelAliasId));
      if (!row) {
        return { state: "blocked", reason: "missing_alias" };
      }
      if (row.ticket) {
        return { state: "ok", ticket: row.ticket, reused: true };
      }
      if (
        !row.dirty
        || row.conflicted
        || !isPositiveInteger(row.basePolicyVersion)
        || (row.draftMode === "quality_cost" && !isRoutingQualityCostConfigValid(row.draftQualityCostConfig))
      ) {
        return { state: "blocked", reason: row.conflicted ? "conflict" : "not_ready" };
      }
      row.operationGeneration += 1;
      const submittedQualityCostConfig = row.draftMode === "quality_cost"
        ? copyQualityCostConfig(row.draftQualityCostConfig)
        : null;
      const fingerprint = JSON.stringify([
        row.modelAliasId,
        row.draftMode,
        submittedQualityCostConfig,
        row.basePolicyVersion,
      ]);
      const requestId = row.retryOperation?.fingerprint === fingerprint
        ? row.retryOperation.requestId
        : createRequestId();
      const controller = new AbortController();
      row.ticket = Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        modelAliasId: row.modelAliasId,
        draftRevision: row.draftRevision,
        routingMode: row.draftMode,
        qualityCostConfig: submittedQualityCostConfig
          ? Object.freeze(submittedQualityCostConfig)
          : null,
        baseVersion: row.basePolicyVersion,
        operationGeneration: row.operationGeneration,
        fingerprint,
        requestId,
        controller,
        signal: controller.signal,
      });
      row.actionError = null;
      row.feedback = "保存中";
      return { state: "ok", ticket: row.ticket, reused: false };
    },

    /** 合法 PATCH 200 以响应为权威值；保存期间发生的 GET 不会使该响应失效。 */
    acceptSuccess(ticket, result, owner) {
      if (!isCurrentOperation(ticket, owner)) {
        return false;
      }
      const row = rows.get(ticket.modelAliasId);
      const routingPolicy = result?.routing_policy;
      const authoritativePolicyId = text(row.authoritative?.routing_policy?.routing_policy_id);
      const changed = result?.changed;
      // acquireSave 只会为 dirty 行建 ticket，因此合法成功必然是真实变更并严格 +1；
      // no-op 200 说明响应不属于这个 ticket，不能在同版本下合并另一 mode。
      const expectedVersion = ticket.baseVersion + 1;
      if (
        !routingPolicy
        || changed !== true
        || text(routingPolicy.model_alias_id) !== row.modelAliasId
        || !authoritativePolicyId
        || text(routingPolicy.routing_policy_id) !== authoritativePolicyId
        || routingPolicy.routing_mode !== ticket.routingMode
        || (
          ticket.routingMode === "quality_cost"
          && !qualityCostConfigsEqual(
            normalizeQualityCostConfig(routingPolicy.quality_cost_config, { useDefaults: false }),
            ticket.qualityCostConfig,
          )
        )
        || routingPolicy.status !== "enabled"
        || !isPositiveInteger(routingPolicy.version)
        || routingPolicy.version !== expectedVersion
      ) {
        // 200 仍是不可信网络输入；身份、提交意图或 CAS 算术不一致时交给统一失败态，
        // 不能把另一 Alias/Policy 或伪造版本合并进当前租户的权威行。
        return false;
      }
      const mergedAlias = mergePolicyIntoAlias(row.authoritative, routingPolicy);
      row.ticket = null;
      adoptAuthority(row, mergedAlias);
      row.actionError = null;
      row.feedback = result?.changed === false ? "配置未变化" : "路由策略已保存";
      return true;
    },

    /**
     * 处理确定失败或网络结果未知。
     * 409 进入显式 rebase 门禁；其他失败保留草稿，5xx/网络异常保留 request_id 供安全重试恢复。
     */
    acceptFailure(ticket, error, owner) {
      if (!isCurrentOperation(ticket, owner)) {
        return false;
      }
      const row = rows.get(ticket.modelAliasId);
      row.ticket = null;
      row.actionError = error;
      row.feedback = error?.status === 409 ? "版本已变化，请载入最新版本后重新选择" : "保存失败";
      // 版本冲突必须冻结原草稿并等待显式 rebase，不能静默覆盖服务器策略。
      if (error?.status === 409) {
        row.conflicted = true;
        const conflictPolicy = error?.currentRoutingPolicy;
        // 只接纳同一 Alias 的安全策略摘要，防止错误体中的其他对象跨行覆盖。
        if (conflictPolicy && text(conflictPolicy.model_alias_id) === row.modelAliasId) {
          row.sideAuthoritativeSnapshot = mergePolicyIntoAlias(row.authoritative, conflictPolicy);
          row.sideSnapshotMissing = false;
        }
        row.retryOperation = null;
      } else if (!error?.status || error.status >= 500) {
        row.retryOperation = { fingerprint: ticket.fingerprint, requestId: ticket.requestId };
      } else {
        row.retryOperation = null;
      }
      return true;
    },

    /** 明确取消会 abort 当前 PATCH、推进 operation generation，并采用已知最新服务器事实。 */
    cancel(modelAliasId) {
      const row = rows.get(text(modelAliasId));
      if (!row) {
        return false;
      }
      row.ticket?.controller.abort();
      row.ticket = null;
      row.operationGeneration += 1;
      // dirty/busy/409 期间若 GET 已观察到更新版本，取消草稿必须采用该旁路快照；继续把
      // 本地旧版本标为 clean 会在 UI 中复活已经失效的 Policy 图。
      if (row.sideAuthoritativeSnapshot) {
        const latest = row.sideAuthoritativeSnapshot;
        adoptAuthority(row, latest);
        row.actionError = null;
        row.feedback = "已取消并载入最新版本";
        return true;
      }
      // 权威 GET 已确认 Alias 被删除时直接移除旧行，不能保留一个可再次编辑的幽灵对象。
      if (row.sideSnapshotMissing) {
        rows.delete(row.modelAliasId);
        return true;
      }
      // 409 后若最新 GET 尚未返回，继续保持冲突门禁；短暂等待比恢复旧版本更安全。
      if (row.conflicted) {
        row.actionError = null;
        row.feedback = "正在读取最新版本";
        return true;
      }
      row.draftMode = policyFacts(row.authoritative).mode;
      row.draftQualityCostConfig = copyQualityCostConfig(policyFacts(row.authoritative).qualityCostConfig);
      row.draftRevision += 1;
      row.dirty = false;
      row.conflicted = false;
      row.sideAuthoritativeSnapshot = null;
      row.sideSnapshotMissing = false;
      row.retryOperation = null;
      row.actionError = null;
      row.feedback = "已取消未保存选择";
      return true;
    },

    /** 管理员显式采用 409/后台 GET 的旁路权威值；采用后必须重新选择才能保存。 */
    rebase(modelAliasId) {
      const row = rows.get(text(modelAliasId));
      if (!row || row.ticket || !row.sideAuthoritativeSnapshot) {
        return false;
      }
      const latest = row.sideAuthoritativeSnapshot;
      adoptAuthority(row, latest);
      row.actionError = null;
      row.feedback = "已载入最新版本，请重新选择";
      return true;
    },

    /** 返回不含 AbortController、Token 或 Session 明文的渲染快照。 */
    snapshot() {
      const renderedRows = [...rows.values()].map((row) => ({
        modelAliasId: row.modelAliasId,
        alias: copyAlias(row.authoritative),
        basePolicyVersion: row.basePolicyVersion,
        draftMode: row.draftMode,
        draftQualityCostConfig: copyQualityCostConfig(row.draftQualityCostConfig),
        draftRevision: row.draftRevision,
        dirty: row.dirty,
        busy: Boolean(row.ticket),
        conflicted: row.conflicted,
        sideAuthoritativeSnapshot: row.sideAuthoritativeSnapshot
          ? copyAlias(row.sideAuthoritativeSnapshot)
          : null,
        sideSnapshotMissing: row.sideSnapshotMissing,
        refreshGeneration: row.refreshGeneration,
        operationGeneration: row.operationGeneration,
        actionError: row.actionError,
        feedback: row.feedback,
      }));
      return {
        state: loadError && !renderedRows.length ? "error" : "ok",
        rows: renderedRows,
        refreshError: renderedRows.length ? loadError : null,
      };
    },

    /** 离页、登出、401 或身份变化时销毁全部行和活动 ticket。 */
    invalidate() {
      invalidateRows();
      activeIdentity = "";
      activeNavigation = "";
    },

    /** 暴露只读门禁供主应用在异步回调前复核，不让刷新代次参与 PATCH 判断。 */
    isCurrentOperation,
  };
}

/**
 * 渲染路由策略页。
 * admin 有显式草稿和保存动作，developer 只看到相同安全摘要，viewer 返回空字符串。
 */
export function renderRoutingPolicyPanel(state, authContext) {
  // 缺少后端 routing.read 时不生成任何页面节点，真实直调权限仍由后端执行。
  if (!hasPermission(authContext, PERMISSIONS.ROUTING_READ)) {
    return "";
  }
  if (!state) {
    return renderLoading();
  }
  // 首轮 GET 失败时没有可信版本可编辑，页面必须整体失败关闭。
  if (state.state === "error") {
    return renderFatalError(state.refreshError ?? state);
  }
  const rows = Array.isArray(state.rows) ? state.rows : [];
  const canWrite = hasPermission(authContext, PERMISSIONS.ROUTING_WRITE);
  const cacheAffinityRows = new Map(
    (Array.isArray(state.cacheAffinity?.rows) ? state.cacheAffinity.rows : [])
      .map((row) => [row.modelAliasId, row]),
  );
  return `
    <div class="routing-policy-heading">
      <div>
        <h2 id="routing-policies-title">路由策略</h2>
      </div>
      <span>${escapeHtml(rows.length)} 条策略</span>
    </div>
    ${renderRefreshError(state.refreshError)}
    <div class="routing-policy-table" role="table" aria-label="当前租户路由策略">
      <div class="routing-policy-row routing-policy-head${canWrite ? "" : " routing-policy-readonly-row"}" role="row">
        <span>模型别名 / 运行环境</span>
        <span>参与选择的模型</span>
        <span>选择方式 / 版本</span>
        <span>更新时间</span>
        ${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${rows.length ? rows.map((row) => renderRoutingPolicyRow(row, canWrite, cacheAffinityRows.get(row.modelAliasId))).join("") : renderEmptyRow()}
    </div>
  `;
}

/** 渲染单条 Alias 策略；静态候选明确标为配置事实，不承诺运行时一定选中。 */
function renderRoutingPolicyRow(row, canWrite, cacheAffinityRow) {
  const alias = row.alias ?? {};
  const policy = policyFacts(alias);
  const sideVersion = policyFacts(row.sideAuthoritativeSnapshot).version;
  // mode-only 更新不会改 Alias 行时间，页面应优先展示 RoutingPolicy 自身的更新时间。
  const updatedAt = alias.routing_policy?.updated_at ?? alias.updated_at;
  return `
    <div class="routing-policy-row${canWrite ? "" : " routing-policy-readonly-row"}${row.busy ? " is-busy" : ""}${row.conflicted ? " is-conflicted" : ""}" role="row" data-routing-policy-row="${escapeAttribute(row.modelAliasId)}">
      <span class="routing-policy-identity" data-label="模型别名 / 运行环境">
        <strong>${escapeHtml(alias.alias ?? row.modelAliasId)}</strong>
        <em>${escapeHtml(alias.environment_name ?? alias.environment ?? "-")}</em>
      </span>
      <span class="routing-policy-candidates" data-label="参与选择的模型">
        ${renderConfiguredCandidates(alias)}
      </span>
      <span class="routing-policy-current" data-label="选择方式 / 版本">
        <strong>${escapeHtml(modeLabel(policy.mode))}</strong>
        <em>${escapeHtml(modeDescription(policy.mode))}</em>
        <small class="routing-policy-version">${policy.version == null ? "尚未保存策略" : `配置版本 v${escapeHtml(policy.version)}`}</small>
        ${policy.mode === "quality_cost" ? renderQualityCostSummary(policy.qualityCostConfig) : ""}
        ${sideVersion != null && sideVersion !== row.basePolicyVersion ? `<small class="routing-policy-side-version">服务器已有 v${escapeHtml(sideVersion)}</small>` : ""}
      </span>
      <time data-label="更新时间" datetime="${escapeAttribute(updatedAt ?? "")}">${escapeHtml(formatDateTime(updatedAt))}</time>
      ${canWrite ? `<span class="routing-policy-action" data-label="操作">${renderRoutingPolicyEditor(row)}</span>` : ""}
      ${renderRowFeedback(row)}
    </div>
    ${renderCacheAffinityRow(cacheAffinityRow, canWrite)}
  `;
}

/** 渲染 admin 的四段模式控件、质量成本配置和显式保存动作；developer 路径不会调用本函数。 */
function renderRoutingPolicyEditor(row) {
  const qualityConfigValid = row.draftMode !== "quality_cost"
    || isRoutingQualityCostConfigValid(row.draftQualityCostConfig);
  const cannotSave = !row.dirty
    || row.busy
    || row.conflicted
    || !isPositiveInteger(row.basePolicyVersion)
    || !qualityConfigValid;
  return `
    <form data-routing-policy-form data-model-alias-id="${escapeAttribute(row.modelAliasId)}" aria-busy="${row.busy ? "true" : "false"}">
      <fieldset class="routing-mode-segments" ${row.busy || row.conflicted ? "disabled" : ""}>
        <legend>选择方式</legend>
        ${ROUTING_MODES.map((mode) => `
          <label title="${escapeAttribute(modeDescription(mode))}">
            <input type="radio" name="routingMode" value="${escapeAttribute(mode)}" ${row.draftMode === mode ? "checked" : ""} />
            <span>${escapeHtml(modeLabel(mode))}</span>
          </label>
        `).join("")}
      </fieldset>
      ${renderQualityCostEditor(row)}
      <div class="routing-policy-buttons">
        <button type="submit" ${cannotSave ? "disabled" : ""}>${row.busy ? "保存中" : "保存"}</button>
        <button type="button" data-routing-policy-cancel ${!row.dirty && !row.busy && !row.conflicted ? "disabled" : ""}>取消</button>
        ${row.conflicted && row.sideAuthoritativeSnapshot ? `<button type="button" data-routing-policy-rebase>载入最新版本</button>` : ""}
      </div>
    </form>
  `;
}

/** 渲染 quality_cost 的内部配置载荷；组织用户只需选择模式，不暴露评分调参。 */
function renderQualityCostEditor(row) {
  const config = row.draftQualityCostConfig ?? {};
  return `
    <div class="routing-quality-cost-config" data-routing-quality-config ${row.draftMode === "quality_cost" ? "" : "hidden"}>
      <input type="hidden" name="qualityWeightPercent" value="${escapeAttribute(bpsInputValue(config.quality_weight_bps))}" />
      <input type="hidden" name="costWeightPercent" value="${escapeAttribute(bpsInputValue(config.cost_weight_bps))}" />
      <input type="hidden" name="latencyWeightPercent" value="${escapeAttribute(bpsInputValue(config.latency_weight_bps))}" />
      <input type="hidden" name="qualityFloorPercent" value="${escapeAttribute(bpsInputValue(config.quality_floor_bps))}" />
      <input type="hidden" name="minimumSampleCount" value="${escapeAttribute(integerInputValue(config.minimum_sample_count))}" />
      <p class="routing-quality-cost-note">系统自动综合质量、成本和响应速度排序。</p>
    </div>
  `;
}

/** developer 和 admin 都可读取当前冻结在 RoutingPolicy 上的配置，不从当前评分回算。 */
function renderQualityCostSummary(config) {
  if (!isRoutingQualityCostConfigValid(config)) {
    return `<small>质量成本配置不可用</small>`;
  }
  return `<small class="routing-quality-cost-summary">系统自动综合质量、成本和响应速度</small>`;
}

/** 候选优先消费后端安全摘要；旧响应缺摘要时只回退到 Alias 现有主备显示字段。 */
function renderConfiguredCandidates(alias) {
  const candidates = configuredCandidates(alias);
  if (!candidates.length) {
    return `<em>未返回候选摘要</em>`;
  }
  const primary = candidates.find((candidate) => candidate.role === "default") ?? candidates[0];
  const fallbacks = candidates.filter((candidate) => candidate !== primary);
  const primaryHtml = `
    <span class="routing-policy-candidate routing-policy-primary-candidate">
      <small>主模型</small>
      <strong>${escapeHtml(primary.modelName)}</strong>
      <em>${escapeHtml(primary.providerName)}</em>
    </span>
  `;
  if (!fallbacks.length) {
    return `<div class="routing-policy-candidate-summary">${primaryHtml}<span class="routing-policy-no-fallback">未配置备用模型</span></div>`;
  }
  return `
    <div class="routing-policy-candidate-summary">
      ${primaryHtml}
      <details class="routing-policy-fallback-details">
        <summary>备用模型 ${fallbacks.length} 个</summary>
        <ol>
          ${fallbacks.map((candidate) => `
            <li>
              <strong>${escapeHtml(candidate.modelName)}</strong>
              <em>${escapeHtml(candidate.providerName)}</em>
            </li>
          `).join("")}
        </ol>
      </details>
    </div>
  `;
}

/** 把当前 DTO 的候选白名单字段归一为完整有序候选，不解析任意 JSON。 */
function configuredCandidates(alias) {
  const source = Array.isArray(alias?.routing_candidates)
    ? alias.routing_candidates
    : Array.isArray(alias?.routing_policy?.candidates)
      ? alias.routing_policy.candidates
      : [];
  const normalized = source.map((item, index) => ({
    role: text(item?.candidate_role ?? item?.role ?? (index === 0 ? "default" : "fallback")),
    // display_name 不是必填；内部模型别名和上游模型名仍是可读业务标识，
    // 只有全部缺失时才显示固定占位，绝不把技术 UUID 当名称。
    modelName: text(
      item?.display_name
      ?? item?.model_name
      ?? item?.internal_model_id
      ?? item?.provider_model_id
      ?? "未命名模型",
    ),
    providerName: text(item?.provider_name ?? item?.provider ?? "未命名服务"),
  }));
  if (normalized.length) {
    return normalized;
  }
  const fallback = [{
    role: "default",
    modelName: text(
      alias?.default_display_name
      ?? alias?.default_internal_model_id
      ?? alias?.default_provider_model_id
      ?? "未命名模型",
    ),
    providerName: text(alias?.default_provider_name ?? "未命名服务"),
  }];
  // 只有服务端明确配置备用目标时才派生第二项，空 fallback 不生成虚构候选。
  if (alias?.fallback_model_id || alias?.fallback_internal_model_id) {
    fallback.push({
      role: "fallback",
      modelName: text(
        alias?.fallback_display_name
        ?? alias?.fallback_internal_model_id
        ?? alias?.fallback_provider_model_id
        ?? "未命名模型",
      ),
      providerName: text(alias?.fallback_provider_name ?? "未命名服务"),
    });
  }
  return fallback;
}

/** 渲染行级 live feedback；错误只显示统一 code/message/request_id，不输出响应原文。 */
function renderRowFeedback(row) {
  if (row.actionError) {
    return `
      <div class="routing-policy-row-feedback error-state" role="alert" tabindex="-1">
        <strong>${escapeHtml(row.actionError.code ?? "model_alias.routing_policy_update_failed")}</strong>
        <span>${escapeHtml(row.actionError.message ?? "路由策略保存失败")}</span>
        <code>request_id: ${escapeHtml(row.actionError.requestId ?? row.actionError.request_id ?? "-")}</code>
      </div>
    `;
  }
  return row.feedback
    ? `<div class="routing-policy-row-feedback" role="status" aria-live="polite" tabindex="-1">${escapeHtml(row.feedback)}</div>`
    : "";
}

/** 初次读取前渲染稳定占位，不伪造策略版本或候选。 */
function renderLoading() {
  return `
    <div class="routing-policy-heading">
      <div><h2 id="routing-policies-title">路由策略</h2><p>正在读取模型别名的路由设置。</p></div>
    </div>
    <div class="empty-state">等待 /model-aliases 响应。</div>
  `;
}

/** 初次读取失败时只展示统一错误和 request_id。 */
function renderFatalError(error) {
  return `
    <div class="routing-policy-heading">
      <div><h2 id="routing-policies-title">路由策略</h2><p>路由策略暂不可用。</p></div>
    </div>
    <div class="empty-state error-state" role="alert">
      ${escapeHtml(error?.message ?? "路由策略读取失败")} request_id: ${escapeHtml(error?.requestId ?? "-")}
    </div>
  `;
}

/** 后台刷新失败不销毁已有草稿，仅提供可追踪的非阻断告警。 */
function renderRefreshError(error) {
  return error
    ? `<div class="routing-policy-refresh-error" role="status">后台刷新失败，当前草稿已保留。request_id: ${escapeHtml(error.requestId ?? "-")}</div>`
    : "";
}

/** 渲染空租户状态。 */
function renderEmptyRow() {
  return `<div class="routing-policy-row routing-policy-empty" role="row"><span>当前组织还没有模型别名。</span></div>`;
}

/** 从 Alias 或 PATCH policy 响应提取有效模式和版本；缺行保持 nullable implicit fixed 合同。 */
function policyFacts(alias) {
  const policy = alias?.routing_policy;
  const candidateMode = text(policy?.routing_mode ?? alias?.effective_routing_mode ?? "fixed");
  const mode = ROUTING_MODES.includes(candidateMode) ? candidateMode : candidateMode || "fixed";
  return {
    mode,
    version: isPositiveInteger(policy?.version) ? Number(policy.version) : null,
    // 旧响应未带休眠配置时只为旧模式采用迁移默认值；活动 quality_cost 缺字段必须显示为无效并关闭保存。
    qualityCostConfig: normalizeQualityCostConfig(policy?.quality_cost_config, {
      useDefaults: mode !== "quality_cost" && !policy?.quality_cost_config,
    }),
  };
}

/** 比较当前可提交草稿与权威 Policy；旧模式不执行也不提交休眠质量配置。 */
function routingPolicyDraftIsDirty(row) {
  const authoritative = policyFacts(row.authoritative);
  if (row.draftMode !== authoritative.mode) {
    return true;
  }
  return row.draftMode === "quality_cost"
    && !qualityCostConfigsEqual(row.draftQualityCostConfig, authoritative.qualityCostConfig);
}

/**
 * 归一化五字段质量成本配置。
 * useDefaults 仅兼容 PRD-048 前的旧模式响应；活动 quality_cost 或用户输入绝不静默补齐缺字段。
 */
function normalizeQualityCostConfig(config, { useDefaults = false } = {}) {
  const normalized = {};
  for (const field of QUALITY_COST_CONFIG_FIELDS) {
    const value = config?.[field];
    normalized[field] = isNonnegativeSafeInteger(value)
      ? Number(value)
      : useDefaults
        ? DEFAULT_QUALITY_COST_CONFIG[field]
        : null;
  }
  return normalized;
}

/** 复制配置为普通对象，防止 authoritative、草稿、ticket 和旁路快照共享可变引用。 */
function copyQualityCostConfig(config) {
  return Object.fromEntries(QUALITY_COST_CONFIG_FIELDS.map((field) => [field, config?.[field] ?? null]));
}

/** 精确比较五个 bps/样本字段，不让未知属性参与幂等意图或 dirty 判断。 */
function qualityCostConfigsEqual(left, right) {
  return QUALITY_COST_CONFIG_FIELDS.every((field) => left?.[field] === right?.[field]);
}

/**
 * 校验 PAGE-068 的完整 quality_cost 配置。
 * 质量权重和门槛必须大于 0，三权重必须精确等于 10000；后端仍会重复执行同一约束。
 */
export function isRoutingQualityCostConfigValid(config) {
  return isIntegerInRange(config?.quality_weight_bps, 1, 10000)
    && isIntegerInRange(config?.cost_weight_bps, 0, 10000)
    && isIntegerInRange(config?.latency_weight_bps, 0, 10000)
    && isIntegerInRange(config?.quality_floor_bps, 1, 10000)
    && isIntegerInRange(config?.minimum_sample_count, 1, 10000)
    && config.quality_weight_bps + config.cost_weight_bps + config.latency_weight_bps === 10000;
}

/** 把百分比字符串精确转换为 0..10000 bps，不使用 parseFloat 或浮点乘法。 */
export function routingQualityPercentToBps(value) {
  const normalized = text(value).trim();
  if (!/^(?:100(?:\.0{1,2})?|\d{1,2}(?:\.\d{1,2})?)$/.test(normalized)) {
    return null;
  }
  const [whole, fraction = ""] = normalized.split(".");
  const bps = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return isIntegerInRange(bps, 0, 10000) ? bps : null;
}

/** 把最小样本输入收敛为 1..10000 的十进制整数，拒绝指数、符号、小数和前导空白。 */
export function routingMinimumSampleCount(value) {
  const normalized = text(value).trim();
  if (!/^(?:[1-9]\d{0,3}|10000)$/.test(normalized)) {
    return null;
  }
  const count = Number(normalized);
  return isIntegerInRange(count, 1, 10000) ? count : null;
}

/** 将 bps 转为百分比输入/展示文本，整数拆分避免 0.1 浮点表示误差。 */
function bpsInputValue(value) {
  if (!isIntegerInRange(value, 0, 10000)) {
    return "";
  }
  const whole = Math.floor(value / 100);
  const fraction = String(value % 100).padStart(2, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}`;
}

/** 百分比展示复用输入的精确整数拆分，始终显式追加 % 单位。 */
function formatBps(value) {
  if (!isNonnegativeSafeInteger(value)) {
    return "-";
  }
  const whole = Math.floor(value / 100);
  const fraction = String(value % 100).padStart(2, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}%`;
}

/** 最小样本输入只接受正整数；无效值显示为空而不是伪造默认值。 */
function integerInputValue(value) {
  return isIntegerInRange(value, 1, 10000) ? String(value) : "";
}

/** 安全整数范围校验拒绝布尔、字符串和浮点，保持与后端严格 JSON number 合同一致。 */
function isIntegerInRange(value, minimum, maximum) {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= minimum
    && value <= maximum;
}

/** 非负安全整数守卫供权重合计和 DTO 归一化使用。 */
function isNonnegativeSafeInteger(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

/** 复制只读响应对象，避免旁路快照和 authoritative 引用同一可变数组。 */
function copyAlias(alias) {
  if (!alias || typeof alias !== "object" || Array.isArray(alias)) {
    return {};
  }
  const policy = alias.routing_policy && typeof alias.routing_policy === "object"
    ? {
        ...alias.routing_policy,
        quality_cost_config: alias.routing_policy.quality_cost_config
          ? { ...alias.routing_policy.quality_cost_config }
          : alias.routing_policy.quality_cost_config,
        candidates: Array.isArray(alias.routing_policy.candidates)
          ? alias.routing_policy.candidates.map((item) => ({ ...item }))
          : alias.routing_policy.candidates,
        warnings: Array.isArray(alias.routing_policy.warnings)
          ? [...alias.routing_policy.warnings]
          : alias.routing_policy.warnings,
      }
    : alias.routing_policy ?? null;
  return {
    ...alias,
    routing_policy: policy,
    routing_candidates: Array.isArray(alias.routing_candidates)
      ? alias.routing_candidates.map((item) => ({ ...item }))
      : alias.routing_candidates,
  };
}

/** 用 PATCH 返回的完整 RoutingPolicy 更新 Alias 权威摘要，不猜测其他 Alias 字段。 */
function mergePolicyIntoAlias(alias, routingPolicy) {
  const candidates = Array.isArray(routingPolicy?.candidates)
    ? routingPolicy.candidates.map((item) => ({ ...item }))
    : [];
  return {
    ...copyAlias(alias),
    routing_policy: {
      ...routingPolicy,
      quality_cost_config: routingPolicy?.quality_cost_config
        ? { ...routingPolicy.quality_cost_config }
        : routingPolicy?.quality_cost_config,
      candidates,
      warnings: Array.isArray(routingPolicy?.warnings) ? [...routingPolicy.warnings] : [],
    },
    routing_candidates: candidates,
    effective_routing_mode: routingPolicy?.routing_mode ?? alias?.effective_routing_mode,
    routing_policy_implicit: false,
    updated_at: routingPolicy?.updated_at ?? alias?.updated_at,
  };
}

/** 正整数校验拒绝布尔值、浮点数和字符串，保持与后端严格 CAS 合同一致。 */
function isPositiveInteger(value) {
  // JSON number 超过安全整数后无法精确表达 CAS 版本，前端必须失败关闭而不是近似提交。
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

/** 将模式枚举转换为固定中文；未知值保持转义后的原枚举，便于发现坏数据。 */
function modeLabel(mode) {
  return ROUTING_MODE_LABELS[mode] ?? mode ?? "-";
}

function modeDescription(mode) {
  return ROUTING_MODE_DESCRIPTIONS[mode] ?? "按当前配置选择候选模型";
}

/** 将候选角色转换为配置语义，避免把初始 fallback 角色误写成“已触发 Fallback”。 */
function candidateRoleLabel(role) {
  return role === "fallback" ? "备用模型" : role === "default" ? "优先模型" : role || "-";
}

/** 格式化秒级更新时间；空值保持短横线。 */
function formatDateTime(value) {
  return value ? text(value).replace("T", " ").replace(/\.\d+Z?$/, "Z") : "-";
}

/** 把任意可空 DTO 字段收敛为字符串，后续统一执行 HTML 转义。 */
function text(value) {
  return value == null ? "" : String(value);
}

/** 转义 HTML 文本上下文，保护 Alias、Provider 和统一错误消息。 */
function escapeHtml(value) {
  return text(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 在文本转义基础上补反引号，保护 data-* 和 datetime 属性。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
