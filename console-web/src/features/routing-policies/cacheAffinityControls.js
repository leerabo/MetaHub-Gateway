/**
 * 模块职责：维护 PAGE-072 Prompt Cache 亲和配置的行级草稿、CAS 保存生命周期和安全渲染。
 * 安全边界：状态只接纳当前身份与租户的 Alias 摘要；Prompt、Binding、Credential、摘要和 Key 永不进入 DOM。
 */

import { createCacheAffinityRequestId } from "../../lib/modelAliases.js";

// 配置字段、数值范围和 TTL 预设与 Control API/0040 迁移保持一致，前端校验不能替代后端约束。
const CACHE_AFFINITY_STATUSES = new Set(["enabled", "disabled"]);
const DATA_CLASS_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const MIN_PREFIX_CHARS = 256;
const MAX_PREFIX_CHARS = 16384;
const MIN_TTL_SECONDS = 60;
const MAX_TTL_SECONDS = 86400;
const DEFAULT_CACHE_AFFINITY = Object.freeze({
  status: "disabled",
  data_class: null,
  minimum_prefix_chars: 1024,
  ttl_seconds: 3600,
});
// TTL 选项限制管理员只能选择产品约定周期；服务端已有非预设值会在渲染时单独保留。
const CACHE_AFFINITY_TTL_OPTIONS = Object.freeze([
  Object.freeze({ value: 300, label: "5 分钟" }),
  Object.freeze({ value: 3600, label: "1 小时" }),
  Object.freeze({ value: 21600, label: "6 小时" }),
  Object.freeze({ value: 86400, label: "24 小时" }),
]);

/**
 * 创建独立于 RoutingPolicy 的 CacheAffinity 页面内存。
 * refresh generation 只淘汰 GET，operation generation 负责取消 PATCH；两类代次不能互相替代。
 */
export function createCacheAffinityPageMemory({ requestIdFactory } = {}) {
  const createRequestId = requestIdFactory ?? createCacheAffinityRequestId;
  const rows = new Map();
  let activeIdentity = "";
  let activeNavigation = "";
  let refreshSequence = 0;
  let loadError = null;

  /** 身份或导航变化时同步销毁全部草稿和网络所有权。 */
  function enter(identity, navigation) {
    const nextIdentity = cacheAffinityText(identity);
    const nextNavigation = cacheAffinityText(navigation);
    if (nextIdentity === activeIdentity && nextNavigation === activeNavigation) {
      return;
    }
    invalidateRows();
    activeIdentity = nextIdentity;
    activeNavigation = nextNavigation;
  }

  /** Abort 活动 PATCH 并推进代次，fetch 即使忽略 Abort 也无法回写新身份。 */
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

  /** 核对 GET ticket 仍属于当前完整身份、导航和最后一轮刷新。 */
  function isCurrentRefresh(ticket) {
    return Boolean(
      ticket
      && ticket.identity === activeIdentity
      && ticket.navigation === activeNavigation
      && ticket.generation === refreshSequence,
    );
  }

  /** 核对 PATCH ticket 仍由当前行和当前身份持有，不让后台 GET 代次误杀合法保存。 */
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
      && ticket.identity === cacheAffinityText(owner.identity ?? activeIdentity)
      && ticket.navigation === cacheAffinityText(owner.navigation ?? activeNavigation),
    );
  }

  return {
    /** 开始一轮复用 ModelAlias 列表的读取，并冻结当前身份 owner。 */
    beginRefresh({ identity, navigation }) {
      enter(identity, navigation);
      const generation = ++refreshSequence;
      return Object.freeze({ identity: activeIdentity, navigation: activeNavigation, generation });
    },

    /**
     * 接纳最新 Alias 列表。
     * dirty/busy/conflict 行只保存旁路权威值，不能让定时刷新覆盖草稿或改变 PATCH base version。
     */
    acceptRefresh(ticket, aliases) {
      if (!isCurrentRefresh(ticket)) {
        return false;
      }
      const identityTenantId = tenantIdFromIdentity(ticket.identity);
      const seen = new Set();
      for (const alias of Array.isArray(aliases) ? aliases : []) {
        const modelAliasId = cacheAffinityText(alias?.model_alias_id);
        const tenantId = cacheAffinityText(alias?.tenant_id);
        // 缺失租户或与身份不一致的响应不得进入页面状态，即使后端本应已经隔离。
        if (!modelAliasId || !tenantId || tenantId !== identityTenantId) {
          continue;
        }
        seen.add(modelAliasId);
        const row = rows.get(modelAliasId);
        if (!row) {
          rows.set(modelAliasId, createCacheAffinityRow(alias));
          continue;
        }
        if (row.dirty || row.ticket || row.conflicted) {
          row.sideAuthoritative = cacheAffinityAuthorityFromAlias(alias);
          row.sideAuthorityObserved = true;
          row.sideAliasMissing = false;
        } else {
          adoptCacheAffinityAuthority(row, alias);
        }
      }
      for (const [modelAliasId, row] of rows) {
        if (seen.has(modelAliasId)) {
          continue;
        }
        // 正在编辑的 Alias 被并发删除时保留草稿并冻结保存，取消后才移除旧行。
        if (row.dirty || row.ticket || row.conflicted) {
          row.sideAuthoritative = null;
          row.sideAuthorityObserved = false;
          row.sideAliasMissing = true;
        } else {
          rows.delete(modelAliasId);
        }
      }
      loadError = null;
      return true;
    },

    /** 最新 GET 失败只记录安全错误；已有草稿和权威配置继续保留。 */
    acceptRefreshError(ticket, error) {
      if (!isCurrentRefresh(ticket)) {
        return false;
      }
      loadError = safeCacheAffinityError(error, "Prompt Cache 亲和配置读取失败");
      return true;
    },

    /** 更新完整四字段草稿；非法输入保留为 null 以关闭保存，不伪造默认值。 */
    selectDraft(modelAliasId, draft) {
      const row = rows.get(cacheAffinityText(modelAliasId));
      if (!row || row.ticket || row.conflicted) {
        return false;
      }
      row.draft = normalizeCacheAffinityDraft(draft);
      row.draftRevision += 1;
      row.dirty = !cacheAffinityDraftsEqual(row.draft, draftFromAuthority(row.authoritative));
      row.actionError = null;
      row.feedback = row.dirty ? "存在未保存的亲和配置" : "";
      if (!row.dirty) {
        row.retryOperation = null;
      }
      return true;
    },

    /** 创建唯一 PATCH ticket；同一活动提交复用 ticket，结果未知的同一意图复用 request_id。 */
    acquireSave({ identity, navigation, modelAliasId }) {
      enter(identity, navigation);
      const row = rows.get(cacheAffinityText(modelAliasId));
      if (!row) {
        return { state: "blocked", reason: "missing_alias" };
      }
      if (row.ticket) {
        return { state: "ok", ticket: row.ticket, reused: true };
      }
      if (
        !row.dirty
        || row.conflicted
        || !isCacheAffinityBaseVersion(row.baseVersion, row.authoritative)
        || !isCacheAffinityDraftValid(row.draft)
      ) {
        return { state: "blocked", reason: row.conflicted ? "conflict" : "not_ready" };
      }
      row.operationGeneration += 1;
      const fingerprint = JSON.stringify([
        row.tenantId,
        row.modelAliasId,
        row.draft.status,
        row.draft.data_class,
        row.draft.minimum_prefix_chars,
        row.draft.ttl_seconds,
        row.baseVersion,
      ]);
      const requestId = row.retryOperation?.fingerprint === fingerprint
        ? row.retryOperation.requestId
        : createRequestId();
      const controller = new AbortController();
      row.ticket = Object.freeze({
        identity: activeIdentity,
        navigation: activeNavigation,
        tenantId: row.tenantId,
        modelAliasId: row.modelAliasId,
        draftRevision: row.draftRevision,
        draft: Object.freeze(copyCacheAffinityDraft(row.draft)),
        baseVersion: row.baseVersion,
        operationGeneration: row.operationGeneration,
        fingerprint,
        requestId,
        controller,
        signal: controller.signal,
      });
      row.actionError = null;
      row.feedback = "亲和配置保存中";
      return { state: "ok", ticket: row.ticket, reused: false };
    },

    /** 只接纳与 tenant、Alias、意图、request_id 和 CAS +1 全部一致的 PATCH 200。 */
    acceptSuccess(ticket, result, owner) {
      if (!isCurrentOperation(ticket, owner)) {
        return false;
      }
      const row = rows.get(ticket.modelAliasId);
      const authority = normalizeCacheAffinityAuthority(result?.cache_affinity);
      if (
        result?.changed !== true
        || cacheAffinityText(result?.request_id) !== ticket.requestId
        || !cacheAffinityAuthorityIsValid(authority)
        || authority.tenant_id !== ticket.tenantId
        || authority.model_alias_id !== ticket.modelAliasId
        || authority.version !== ticket.baseVersion + 1
        || !cacheAffinityDraftsEqual(draftFromAuthority(authority), ticket.draft)
      ) {
        // 成功响应同样是不可信网络输入，任一所有权或版本不一致都不能合并到当前租户。
        return false;
      }
      row.ticket = null;
      row.authoritative = authority;
      row.baseVersion = authority.version;
      row.draft = draftFromAuthority(authority);
      row.draftRevision += 1;
      row.dirty = false;
      row.conflicted = false;
      row.sideAuthoritative = null;
      row.sideAuthorityObserved = false;
      row.sideAliasMissing = false;
      row.retryOperation = null;
      row.actionError = null;
      row.feedback = "Prompt Cache 亲和配置已保存";
      return true;
    },

    /** 409 冻结草稿等待显式载入；网络/5xx 保留同一指纹的 request_id 供安全重试。 */
    acceptFailure(ticket, error, owner) {
      if (!isCurrentOperation(ticket, owner)) {
        return false;
      }
      const row = rows.get(ticket.modelAliasId);
      row.ticket = null;
      row.actionError = safeCacheAffinityError(error, "Prompt Cache 亲和配置保存失败");
      row.feedback = error?.status === 409 ? "版本已变化，请载入最新配置后重新编辑" : "亲和配置保存失败";
      // 409 表示权威版本已经变化，必须冻结当前草稿并等待管理员显式载入最新配置。
      if (error?.status === 409) {
        row.conflicted = true;
        const current = normalizeCacheAffinityAuthority(error?.currentCacheAffinity);
        // 冲突错误只接纳同租户同 Alias 的安全配置摘要，其他 details 一律丢弃。
        if (
          cacheAffinityAuthorityIsValid(current)
          && current.tenant_id === row.tenantId
          && current.model_alias_id === row.modelAliasId
        ) {
          row.sideAuthoritative = current;
          row.sideAuthorityObserved = true;
          row.sideAliasMissing = false;
        }
        row.retryOperation = null;
      } else if (!error?.status || error.status >= 500) {
        row.retryOperation = { fingerprint: ticket.fingerprint, requestId: ticket.requestId };
      } else {
        row.retryOperation = null;
      }
      return true;
    },

    /** 取消活动请求并采用旁路权威值；冲突后没有最新 GET 时继续失败关闭。 */
    cancel(modelAliasId) {
      const row = rows.get(cacheAffinityText(modelAliasId));
      if (!row) {
        return false;
      }
      row.ticket?.controller.abort();
      row.ticket = null;
      row.operationGeneration += 1;
      if (row.sideAliasMissing) {
        rows.delete(row.modelAliasId);
        return true;
      }
      // 后台刷新已观察到新权威值时，取消操作应采用该值，避免恢复已经过期的基础版本。
      if (row.sideAuthorityObserved) {
        adoptCacheAffinityValue(row, row.sideAuthoritative);
        row.feedback = "已取消并载入最新配置";
        return true;
      }
      if (row.conflicted) {
        row.actionError = null;
        row.feedback = "正在读取最新配置";
        return true;
      }
      row.draft = draftFromAuthority(row.authoritative);
      row.draftRevision += 1;
      row.dirty = false;
      row.retryOperation = null;
      row.actionError = null;
      row.feedback = "已取消未保存的亲和配置";
      return true;
    },

    /** 管理员显式采用 409 后 GET 的最新配置，采用后必须重新编辑才能再次保存。 */
    rebase(modelAliasId) {
      const row = rows.get(cacheAffinityText(modelAliasId));
      if (!row || row.ticket || !row.sideAuthorityObserved) {
        return false;
      }
      adoptCacheAffinityValue(row, row.sideAuthoritative);
      row.feedback = "已载入最新亲和配置，请重新编辑";
      return true;
    },

    /** 返回只包含配置白名单和渲染状态的快照，不暴露 Token、Session 或 AbortController。 */
    snapshot() {
      const renderedRows = [...rows.values()].map((row) => ({
        modelAliasId: row.modelAliasId,
        tenantId: row.tenantId,
        authoritative: copyCacheAffinityAuthority(row.authoritative),
        baseVersion: row.baseVersion,
        draft: copyCacheAffinityDraft(row.draft),
        draftRevision: row.draftRevision,
        dirty: row.dirty,
        busy: Boolean(row.ticket),
        conflicted: row.conflicted,
        sideAuthoritative: copyCacheAffinityAuthority(row.sideAuthoritative),
        sideAuthorityObserved: row.sideAuthorityObserved,
        sideAliasMissing: row.sideAliasMissing,
        operationGeneration: row.operationGeneration,
        actionError: row.actionError ? { ...row.actionError } : null,
        feedback: row.feedback,
      }));
      return {
        state: loadError && !renderedRows.length ? "error" : "ok",
        rows: renderedRows,
        refreshError: renderedRows.length ? loadError : null,
      };
    },

    /** 离页、登出或身份变化时销毁配置状态及全部活动保存。 */
    invalidate() {
      invalidateRows();
      activeIdentity = "";
      activeNavigation = "";
    },

    /** 暴露只读 ticket 门禁供主应用在异步回调前复核。 */
    isCurrentOperation,
  };
}

/** 把 Alias 安全摘要转换为一条独立的 CacheAffinity 编辑行。 */
function createCacheAffinityRow(alias) {
  const row = {
    modelAliasId: cacheAffinityText(alias?.model_alias_id),
    tenantId: cacheAffinityText(alias?.tenant_id),
    authoritative: null,
    baseVersion: 0,
    draft: copyCacheAffinityDraft(DEFAULT_CACHE_AFFINITY),
    draftRevision: 0,
    dirty: false,
    conflicted: false,
    sideAuthoritative: null,
    sideAuthorityObserved: false,
    sideAliasMissing: false,
    operationGeneration: 0,
    ticket: null,
    retryOperation: null,
    actionError: null,
    feedback: "",
  };
  adoptCacheAffinityAuthority(row, alias);
  return row;
}

/** 从 Alias 采用当前权威配置；仅 clean 行和显式 rebase/cancel 可以调用。 */
function adoptCacheAffinityAuthority(row, alias) {
  row.tenantId = cacheAffinityText(alias?.tenant_id);
  row.modelAliasId = cacheAffinityText(alias?.model_alias_id);
  adoptCacheAffinityValue(row, cacheAffinityAuthorityFromAlias(alias));
}

/** 采用可空配置值并重置草稿、冲突和重试状态。 */
function adoptCacheAffinityValue(row, authority) {
  row.authoritative = copyCacheAffinityAuthority(authority);
  row.baseVersion = authority === null
    ? 0
    : cacheAffinityAuthorityIsValid(authority)
      ? authority.version
      : null;
  row.draft = draftFromAuthority(row.authoritative);
  row.draftRevision += 1;
  row.dirty = false;
  row.conflicted = false;
  row.sideAuthoritative = null;
  row.sideAuthorityObserved = false;
  row.sideAliasMissing = false;
  row.retryOperation = null;
  row.actionError = null;
  row.feedback = "";
}

/** 只从 Alias 的 cache_affinity 字段读取固定白名单，其他运行时属性不会进入内存。 */
function cacheAffinityAuthorityFromAlias(alias) {
  return alias?.cache_affinity == null ? null : normalizeCacheAffinityAuthority(alias.cache_affinity);
}

/** 把服务端对象压缩为配置 DTO 白名单；非法标量保留 null 使编辑失败关闭。 */
function normalizeCacheAffinityAuthority(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return {
    cache_affinity_id: cacheAffinityText(value.cache_affinity_id),
    tenant_id: cacheAffinityText(value.tenant_id),
    model_alias_id: cacheAffinityText(value.model_alias_id),
    status: CACHE_AFFINITY_STATUSES.has(value.status) ? value.status : null,
    data_class: cacheAffinityDataClassValue(value.data_class),
    minimum_prefix_chars: strictInteger(value.minimum_prefix_chars, MIN_PREFIX_CHARS, MAX_PREFIX_CHARS),
    ttl_seconds: strictInteger(value.ttl_seconds, MIN_TTL_SECONDS, MAX_TTL_SECONDS),
    version: strictInteger(value.version, 1, Number.MAX_SAFE_INTEGER),
    created_at: cacheAffinityText(value.created_at),
    updated_at: cacheAffinityText(value.updated_at),
  };
}

/** 验证权威 DTO 的身份、字段范围和启用分类约束，防止坏 200 成为页面真相。 */
function cacheAffinityAuthorityIsValid(value) {
  return Boolean(
    value
    && /^caf_[0-9a-f]{32}$/.test(value.cache_affinity_id)
    && value.tenant_id
    && value.model_alias_id
    && CACHE_AFFINITY_STATUSES.has(value.status)
    && (value.data_class === null || DATA_CLASS_PATTERN.test(value.data_class))
    && !(value.status === "enabled" && value.data_class === null)
    && strictInteger(value.minimum_prefix_chars, MIN_PREFIX_CHARS, MAX_PREFIX_CHARS) !== null
    && strictInteger(value.ttl_seconds, MIN_TTL_SECONDS, MAX_TTL_SECONDS) !== null
    && strictInteger(value.version, 1, Number.MAX_SAFE_INTEGER) !== null
    && value.created_at
    && value.updated_at,
  );
}

/** 把可空权威配置转换为四字段草稿；未配置时使用 0040 默认值但保持 base version=0。 */
function draftFromAuthority(authority) {
  if (authority === null) {
    return copyCacheAffinityDraft(DEFAULT_CACHE_AFFINITY);
  }
  // 已有权威配置必须完整归一化，确保表单草稿不会夹带响应中的运行时敏感字段。
  return normalizeCacheAffinityDraft(authority);
}

/** 归一化表单四字段，空分类转 null，非法整数和状态保留 null。 */
function normalizeCacheAffinityDraft(value) {
  return {
    status: CACHE_AFFINITY_STATUSES.has(value?.status) ? value.status : null,
    data_class: cacheAffinityDataClassValue(value?.data_class),
    minimum_prefix_chars: strictInteger(value?.minimum_prefix_chars, MIN_PREFIX_CHARS, MAX_PREFIX_CHARS),
    ttl_seconds: strictInteger(value?.ttl_seconds, MIN_TTL_SECONDS, MAX_TTL_SECONDS),
  };
}

/** 校验 admin 可提交的完整配置，enabled 必须绑定服务端受控 machine code。 */
export function isCacheAffinityDraftValid(value) {
  return Boolean(
    value
    && CACHE_AFFINITY_STATUSES.has(value.status)
    && (value.data_class === null || DATA_CLASS_PATTERN.test(value.data_class))
    && !(value.status === "enabled" && value.data_class === null)
    && strictInteger(value.minimum_prefix_chars, MIN_PREFIX_CHARS, MAX_PREFIX_CHARS) !== null
    && strictInteger(value.ttl_seconds, MIN_TTL_SECONDS, MAX_TTL_SECONDS) !== null,
  );
}

/** 精确解析最小前缀输入；拒绝小数、指数、bool 和越界值。 */
export function cacheAffinityMinimumPrefixValue(value) {
  return strictDecimalInteger(value, MIN_PREFIX_CHARS, MAX_PREFIX_CHARS);
}

/** 精确解析 TTL option；只有 60..86400 的整数秒可以进入草稿。 */
export function cacheAffinityTtlValue(value) {
  return strictDecimalInteger(value, MIN_TTL_SECONDS, MAX_TTL_SECONDS);
}

/** 比较四个可编辑字段，未知属性和版本不参与 dirty/幂等指纹。 */
function cacheAffinityDraftsEqual(left, right) {
  return left?.status === right?.status
    && left?.data_class === right?.data_class
    && left?.minimum_prefix_chars === right?.minimum_prefix_chars
    && left?.ttl_seconds === right?.ttl_seconds;
}

/** 复制草稿为普通对象，防止 DOM、ticket 和权威状态共享可变引用。 */
function copyCacheAffinityDraft(value) {
  return {
    status: value?.status ?? null,
    data_class: value?.data_class ?? null,
    minimum_prefix_chars: value?.minimum_prefix_chars ?? null,
    ttl_seconds: value?.ttl_seconds ?? null,
  };
}

/** 复制可空权威配置并保持固定白名单，运行时图字段即使存在也会被丢弃。 */
function copyCacheAffinityAuthority(value) {
  return value === null || value === undefined
    ? null
    : {
        cache_affinity_id: value.cache_affinity_id,
        tenant_id: value.tenant_id,
        model_alias_id: value.model_alias_id,
        status: value.status,
        data_class: value.data_class,
        minimum_prefix_chars: value.minimum_prefix_chars,
        ttl_seconds: value.ttl_seconds,
        version: value.version,
        created_at: value.created_at,
        updated_at: value.updated_at,
      };
}

/** 新配置只允许 base version=0，已有配置必须携带正整数版本。 */
function isCacheAffinityBaseVersion(value, authority) {
  return authority === null ? value === 0 : Number.isSafeInteger(value) && value > 0;
}

/** 从 JSON 身份快照提取 tenant_id；畸形身份返回空值并拒绝接纳响应。 */
function tenantIdFromIdentity(identity) {
  try {
    const values = JSON.parse(cacheAffinityText(identity));
    return Array.isArray(values) ? cacheAffinityText(values[0]) : "";
  } catch {
    return "";
  }
}

/**
 * 把表单或 DTO 中的 data_class 归一为去除首尾空格的 machine code。
 * 返回值仍需由精确 pattern 校验；这里只统一 UI、内存与 wire 的空值/空格语义，不放宽内部空格或大小写。
 */
export function cacheAffinityDataClassValue(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const normalized = cacheAffinityText(value).trim();
  return normalized || null;
}

/** 只接受已经是整数的 JavaScript 数值，避免 bool/字符串在服务端前被隐式转换。 */
function strictInteger(value, minimum, maximum) {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= minimum
    && value <= maximum
    ? value
    : null;
}

/** 从浏览器十进制输入精确解析整数，拒绝符号、小数和指数表示。 */
function strictDecimalInteger(value, minimum, maximum) {
  const normalized = cacheAffinityText(value).trim();
  if (!/^\d+$/.test(normalized)) {
    return null;
  }
  return strictInteger(Number(normalized), minimum, maximum);
}

/** 把任意异常压缩成固定展示字段，禁止响应正文、SQL 或敏感 details 进入 DOM。 */
function safeCacheAffinityError(error, fallbackMessage) {
  return {
    status: Number.isInteger(error?.status) ? error.status : 0,
    code: cacheAffinityText(error?.code || "cache_affinity.update_failed"),
    message: cacheAffinityText(error?.message || fallbackMessage),
    requestId: cacheAffinityText(error?.requestId ?? error?.request_id ?? "-"),
  };
}

/**
 * 渲染紧随 Alias RoutingPolicy 行的 Prompt Cache 亲和配置带。
 * admin 获得完整编辑器，developer 只读；调用方对 viewer 不应调用，函数仍不会渲染敏感运行字段。
 */
export function renderCacheAffinityRow(row, canWrite) {
  if (!row) {
    return "";
  }
  const authority = row.authoritative;
  const stateClass = row.busy ? " is-busy" : row.conflicted ? " is-conflicted" : "";
  return `
    <div class="cache-affinity-config-row${stateClass}" role="row" data-cache-affinity-row="${escapeCacheAffinityAttribute(row.modelAliasId)}">
      <div class="cache-affinity-title">
        <strong>Prompt Cache 亲和</strong>
        <span>${escapeCacheAffinityHtml(cacheAffinityStatusLabel(authority?.status, authority))}</span>
      </div>
      ${canWrite ? renderCacheAffinityEditor(row) : renderCacheAffinityReadonly(authority)}
      ${renderCacheAffinityFeedback(row)}
    </div>
  `;
}

/** 渲染 admin 的 toggle、分类、门限、TTL 和显式保存/取消命令。 */
function renderCacheAffinityEditor(row) {
  const draft = row.draft ?? DEFAULT_CACHE_AFFINITY;
  const disabled = row.busy || row.conflicted ? "disabled" : "";
  const valid = isCacheAffinityDraftValid(draft);
  const cannotSave = !row.dirty || row.busy || row.conflicted || !valid;
  // Chrome 按 RegExp v 标志编译 HTML pattern；源码必须写双反斜杠，让 DOM 保留 \-，否则提交校验会抛 SyntaxError。
  return `
    <form class="cache-affinity-form" data-cache-affinity-form data-model-alias-id="${escapeCacheAffinityAttribute(row.modelAliasId)}" aria-busy="${row.busy ? "true" : "false"}">
      <div class="cache-affinity-fields">
        <label class="cache-affinity-toggle">
          <input name="cacheAffinityEnabled" type="checkbox" role="switch" ${draft.status === "enabled" ? "checked" : ""} ${disabled} />
          <span>启用亲和</span>
        </label>
        <label>
          <span>数据分类 code</span>
          <input name="cacheAffinityDataClass" type="text" maxlength="64" pattern="[a-z0-9][a-z0-9._\\-]{0,63}" autocomplete="off" value="${escapeCacheAffinityAttribute(draft.data_class ?? "")}" ${disabled} />
        </label>
        <label>
          <span>最小前缀字符数</span>
          <input name="cacheAffinityMinimumPrefix" type="number" inputmode="numeric" min="256" max="16384" step="1" value="${escapeCacheAffinityAttribute(integerInputValue(draft.minimum_prefix_chars))}" ${disabled} />
        </label>
        <label>
          <span>亲和 TTL</span>
          <select name="cacheAffinityTtl" ${disabled}>${renderCacheAffinityTtlOptions(draft.ttl_seconds)}</select>
        </label>
      </div>
      <div class="cache-affinity-form-footer">
        <output data-cache-affinity-draft-status aria-live="polite">${escapeCacheAffinityHtml(cacheAffinityDraftStatus(row, valid))}</output>
        <div class="routing-policy-buttons">
          <button type="submit" ${cannotSave ? "disabled" : ""}>${row.busy ? "保存中" : "保存"}</button>
          <button type="button" data-cache-affinity-cancel ${!row.dirty && !row.busy && !row.conflicted ? "disabled" : ""}>取消</button>
          ${row.conflicted && row.sideAuthorityObserved ? `<button type="button" data-cache-affinity-rebase>载入最新配置</button>` : ""}
        </div>
      </div>
    </form>
  `;
}

/** developer 只读展示配置摘要；未配置时不伪造 v0 或默认启用状态。 */
function renderCacheAffinityReadonly(authority) {
  if (!authority) {
    return `<div class="cache-affinity-empty">当前 Alias 未配置 Prompt Cache 亲和。</div>`;
  }
  return `
    <dl class="cache-affinity-readonly-grid">
      ${renderCacheAffinityPair("状态", cacheAffinityStatusLabel(authority.status, authority))}
      ${renderCacheAffinityPair("分类", authority.data_class ?? "-")}
      ${renderCacheAffinityPair("最小前缀", `${authority.minimum_prefix_chars ?? "-"} 字符`)}
      ${renderCacheAffinityPair("TTL", cacheAffinityTtlLabel(authority.ttl_seconds))}
      ${renderCacheAffinityPair("版本", authority.version == null ? "-" : `v${authority.version}`)}
      ${renderCacheAffinityPair("更新时间", formatCacheAffinityTime(authority.updated_at))}
    </dl>
  `;
}

/** 为非预设服务端 TTL 注入只代表当前值的 option，编辑其他字段时不会暗改 TTL。 */
function renderCacheAffinityTtlOptions(currentValue) {
  const options = [...CACHE_AFFINITY_TTL_OPTIONS];
  if (
    strictInteger(currentValue, MIN_TTL_SECONDS, MAX_TTL_SECONDS) !== null
    && !options.some((item) => item.value === currentValue)
  ) {
    options.unshift({ value: currentValue, label: `自定义 ${currentValue} 秒` });
  }
  return options.map((item) => `
    <option value="${escapeCacheAffinityAttribute(item.value)}" ${item.value === currentValue ? "selected" : ""}>${escapeCacheAffinityHtml(item.label)}</option>
  `).join("");
}

/** 根据 dirty、输入合法性和冲突状态生成固定高度的表单反馈。 */
function cacheAffinityDraftStatus(row, valid) {
  if (row.conflicted) return "版本冲突，载入最新配置后再编辑";
  if (row.busy) return "正在保存亲和配置";
  if (row.dirty && !valid) return "请检查分类 code、前缀门限和 TTL";
  if (row.dirty) return "存在未保存的亲和配置";
  // clean 行只展示后端确认的版本；未创建配置不能伪造为版本零或已保存状态。
  return row.authoritative ? `当前版本 v${row.baseVersion}` : "尚未创建配置";
}

/** 渲染配置保存反馈；仅使用固定错误字段和 request_id。 */
function renderCacheAffinityFeedback(row) {
  if (row.actionError) {
    return `
      <div class="cache-affinity-feedback error-state" role="alert" tabindex="-1">
        <strong>${escapeCacheAffinityHtml(row.actionError.code)}</strong>
        <span>${escapeCacheAffinityHtml(row.actionError.message)}</span>
        <code>request_id: ${escapeCacheAffinityHtml(row.actionError.requestId)}</code>
      </div>
    `;
  }
  return row.feedback
    ? `<div class="cache-affinity-feedback" role="status" aria-live="polite" tabindex="-1">${escapeCacheAffinityHtml(row.feedback)}</div>`
    : "";
}

/** 渲染只读键值对，所有动态字段统一执行文本转义。 */
function renderCacheAffinityPair(label, value) {
  return `<div><dt>${escapeCacheAffinityHtml(label)}</dt><dd>${escapeCacheAffinityHtml(value ?? "-")}</dd></div>`;
}

/** 状态枚举转换为稳定中文；非法或缺失权威配置显示不可用而不是猜测。 */
function cacheAffinityStatusLabel(status, authority) {
  if (!authority) return "未配置";
  return { enabled: "已启用", disabled: "已禁用" }[status] ?? "配置不可用";
}

/** TTL 展示保留精确秒值，并为四个预设追加便于扫描的中文周期。 */
function cacheAffinityTtlLabel(value) {
  const preset = CACHE_AFFINITY_TTL_OPTIONS.find((item) => item.value === value);
  return preset ? `${preset.label}（${value} 秒）` : value == null ? "-" : `${value} 秒`;
}

/** ISO 时间只做显示级空格替换，不使用本地时区推断或浏览器日期重算。 */
function formatCacheAffinityTime(value) {
  return value ? cacheAffinityText(value).replace("T", " ").replace(/\.\d+Z?$/, "Z") : "-";
}

/** 数值输入只接受已验证整数；null 保持空字符串，不能伪装为零。 */
function integerInputValue(value) {
  return Number.isSafeInteger(value) ? String(value) : "";
}

/** 把可空值转换为文本，避免 undefined 进入请求指纹或 DOM。 */
function cacheAffinityText(value) {
  return value === null || value === undefined ? "" : String(value);
}

/** 对动态文本执行 HTML 转义，禁止分类或错误字段构造可执行标记。 */
function escapeCacheAffinityHtml(value) {
  return cacheAffinityText(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 属性值在 HTML 转义基础上处理反引号，保护 data-* 与 value 边界。 */
function escapeCacheAffinityAttribute(value) {
  return escapeCacheAffinityHtml(value).replaceAll("`", "&#096;");
}
