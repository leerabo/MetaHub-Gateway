// PAGE-120 状态机：全局白标独立于 PAGE-119，按租户、Session 和 RBAC owner 管理刷新与 Logo 生命周期。
import {
  createBrandingObjectUrl,
  normalizeBrandingResponse,
  normalizePublicBrandingResponse,
  revokeBrandingObjectUrl,
} from "../../lib/branding.js";

const PAGE_ID = "branding";
const LANES = Object.freeze(["read", "mutation"]);

/** 生成不依赖活动 hash 的全局读取 owner，供 shell 和登录后刷新共同使用。 */
export function brandingGlobalOwner(authContext) {
  const tenantId = mirroredTenantId(authContext);
  const userId = String(authContext?.user?.user_id ?? "").trim();
  const sessionId = String(authContext?.session?.session_id ?? "").trim();
  if (!tenantId || !userId || !sessionId || !permissionCodes(authContext).includes("branding.read")) return null;
  return Object.freeze({
    page: PAGE_ID, tenantId, userId, sessionId,
    revision: permissionRevision(authContext),
    fingerprint: authorizationFingerprint(authContext),
    navigationOwner: "global",
  });
}

/** 认证/租户/RBAC 任一边界变化都会使全局 Logo 和正文失效。 */
export function brandingIdentitySnapshot(authContext) {
  const owner = brandingGlobalOwner(authContext);
  return owner ? JSON.stringify([owner.tenantId, owner.userId, owner.sessionId, owner.revision, owner.fingerprint]) : "";
}

/** 从认证快照构造白标读取 owner；tenant 镜像不一致时失败关闭。 */
export function brandingPageOwner(authContext, consoleShellState) {
  const tenantId = mirroredTenantId(authContext);
  const userId = String(authContext?.user?.user_id ?? "").trim();
  const sessionId = String(authContext?.session?.session_id ?? "").trim();
  const revision = Number(authContext?.permission_revision ?? authContext?.user?.permission_revision ?? 0);
  const fingerprint = String(authContext?.authorization_fingerprint ?? authContext?.user?.authorization_fingerprint ?? "");
  const item = Array.isArray(consoleShellState?.navigation?.navigation_items)
    ? consoleShellState.navigation.navigation_items.find((candidate) => candidate?.id === PAGE_ID)
    : null;
  const permissions = permissionCodes(authContext);
  if (!tenantId || !userId || !sessionId || !item || item.visible === false || item.disabled === true
    || consoleShellState?.state !== "ok" || consoleShellState.activeNavigationItemId !== PAGE_ID
    || consoleShellState.navigation.active_navigation_item_id !== PAGE_ID
    || !permissions.includes("branding.read")
    || !Array.isArray(item.required_permissions)
    || !item.required_permissions.includes("branding.read")) return null;
  return Object.freeze({
    page: PAGE_ID, tenantId, userId, sessionId, revision, fingerprint,
    canManage: permissions.includes("branding.manage"),
    navigationOwner: JSON.stringify([item.id, item.target_section, item.required_permissions, revision, fingerprint]),
  });
}

/** 创建全局白标状态；清理顺序固定为 DOM/CSS、Blob URL，再 abort 旧请求。 */
export function createBrandingPageState({ documentApi = globalThis.document, clearDom = () => {} } = {}) {
  let ownerKey = "";
  let phase = "idle";
  let branding = null;
  let publicBranding = null;
  let error = null;
  let mutationState = "idle";
  let mutationError = null;
  let feedback = null;
  let logoUrl = "";
  const generations = Object.fromEntries(LANES.map((lane) => [lane, 0]));
  const operations = Object.fromEntries(LANES.map((lane) => [lane, null]));

  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) { clear(); return false; }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    return true;
  }

  function beginRead(owner) {
    if (!owns(owner)) return null;
    phase = branding ? "ready" : "loading";
    error = null;
    return begin("read", owner);
  }

  function acceptRead(ticket, payload, owner, nextLogoUrl = "") {
    if (!isCurrent("read", ticket, owner)) return false;
    const normalized = normalizeBrandingResponse(payload);
    if (!normalized) return false;
    replaceLogo(nextLogoUrl);
    branding = normalized.branding;
    phase = "ready";
    error = null;
    applyBrandingCss(branding, documentApi);
    return true;
  }

  function rejectRead(ticket, nextError, owner) {
    if (!isCurrent("read", ticket, owner)) return false;
    phase = "error";
    error = fixedError(nextError);
    return true;
  }

  function beginMutation(owner) {
    if (!owns(owner) || operations.mutation) return null;
    mutationState = "submitting";
    mutationError = null;
    feedback = null;
    return begin("mutation", owner);
  }

  function acceptMutation(ticket, payload, owner, nextLogoUrl = "") {
    if (!isCurrent("mutation", ticket, owner)) return false;
    const normalized = normalizeBrandingResponse(payload);
    if (!normalized) return false;
    replaceLogo(nextLogoUrl);
    branding = normalized.branding;
    mutationState = "saved";
    mutationError = null;
    feedback = Object.freeze({ requestId: safeChain(payload?.request_id) });
    applyBrandingCss(branding, documentApi);
    return true;
  }

  function rejectMutation(ticket, nextError, owner) {
    if (!isCurrent("mutation", ticket, owner)) return false;
    mutationState = "error";
    mutationError = fixedError(nextError);
    feedback = null;
    return true;
  }

  function setPublicPreview(payload, nextLogoUrl = "") {
    const normalized = normalizePublicBrandingResponse(payload);
    if (!normalized) return false;
    publicBranding = normalized.branding;
    replaceLogo(nextLogoUrl);
    return true;
  }

  function clear() {
    const pending = LANES.map((lane) => operations[lane]);
    ownerKey = "";
    phase = "idle";
    branding = null;
    publicBranding = null;
    error = null;
    mutationState = "idle";
    mutationError = null;
    feedback = null;
    replaceLogo("");
    clearBrandingCss(documentApi);
    try { clearDom(); } finally {
      for (const lane of LANES) { operations[lane] = null; generations[lane] += 1; }
      pending.forEach((ticket) => ticket?.controller.abort());
    }
  }

  /** 离开设置页只取消写操作并清理设置 DOM，保留当前租户全局品牌供 shell 使用。 */
  function leavePage() {
    const mutation = operations.mutation;
    operations.mutation = null;
    generations.mutation += 1;
    mutation?.controller.abort();
    mutationState = "idle";
    mutationError = null;
    feedback = null;
    clearDom();
  }

  function begin(lane, owner) {
    operations[lane]?.controller.abort();
    generations[lane] += 1;
    const ticket = Object.freeze({ lane, generation: generations[lane], ownerKey, controller: new AbortController() });
    operations[lane] = ticket;
    return ticket;
  }

  function isCurrent(lane, ticket, owner) {
    return Boolean(LANES.includes(lane) && ticket && operations[lane] === ticket
      && ticket.generation === generations[lane] && ticket.ownerKey === ownerKey
      && ticket.ownerKey === ownerKeyFor(owner) && !ticket.controller.signal.aborted);
  }

  function finish(ticket) { if (ticket && operations[ticket.lane] === ticket) operations[ticket.lane] = null; }
  function owns(owner) { const key = ownerKeyFor(owner); return Boolean(key && key === ownerKey); }

  function replaceLogo(nextLogoUrl) {
    if (logoUrl && logoUrl !== nextLogoUrl) revokeBrandingObjectUrl(logoUrl);
    logoUrl = typeof nextLogoUrl === "string" && nextLogoUrl.startsWith("blob:") ? nextLogoUrl : "";
  }

  function snapshot() {
    return Object.freeze({
      phase, branding, publicBranding, error, mutationState, mutationError, feedback, logoUrl,
    });
  }

  return Object.freeze({
    enter, beginRead, acceptRead, rejectRead, beginMutation, acceptMutation, rejectMutation,
    setPublicPreview, clear, leavePage, isCurrent, finish, snapshot,
  });
}

/** 把全局合法颜色映射到 CSS 变量；不触碰 PAGE-119 的 --partner-brand-primary。 */
export function applyBrandingCss(branding, documentApi = globalThis.document) {
  const root = documentApi?.documentElement;
  if (!root || !branding) return false;
  if (/^#[0-9A-F]{6}$/.test(String(branding.primary_color ?? ""))) root.style.setProperty("--branding-primary", branding.primary_color);
  if (/^#[0-9A-F]{6}$/.test(String(branding.secondary_color ?? ""))) root.style.setProperty("--branding-secondary", branding.secondary_color);
  return true;
}

/** 清除全局白标变量，伙伴页的局部品牌色由其自身生命周期另行管理。 */
export function clearBrandingCss(documentApi = globalThis.document) {
  documentApi?.documentElement?.style?.removeProperty?.("--branding-primary");
  documentApi?.documentElement?.style?.removeProperty?.("--branding-secondary");
}

/** 允许展示器只接受本地产生的 Blob URL，拒绝远程 URL、javascript/data URL 和 HTML。 */
export function safeBrandingLogoUrl(value) {
  const url = String(value ?? "");
  return /^blob:[a-z][a-z0-9+.-]*:\/\//i.test(url) || /^blob:[^\s]+$/i.test(url) ? url : "";
}

/** 把图片 Blob 交给 state 管理，避免调用方意外长期持有原始二进制。 */
export function brandingLogoUrlFromBlob(blob) { return createBrandingObjectUrl(blob); }

// Keep the lifecycle helper available to the main application through the state module.
export { revokeBrandingObjectUrl };

function ownerKeyFor(owner) {
  if (!owner || owner.page !== PAGE_ID || !owner.tenantId || !owner.userId || !owner.sessionId || !owner.navigationOwner) return "";
  return JSON.stringify([owner.page, owner.tenantId, owner.userId, owner.sessionId, owner.revision, owner.fingerprint]);
}
function mirroredTenantId(authContext) {
  const user = String(authContext?.user?.tenant_id ?? "").trim();
  const tenant = String(authContext?.tenant?.tenant_id ?? "").trim();
  return user && tenant && user === tenant ? tenant : "";
}
function permissionRevision(authContext) { return Number(authContext?.permission_revision ?? authContext?.user?.permission_revision ?? 0); }
function authorizationFingerprint(authContext) { return String(authContext?.authorization_fingerprint ?? authContext?.user?.authorization_fingerprint ?? ""); }
function permissionCodes(authContext) {
  const value = authContext?.permission_codes ?? authContext?.user?.permission_codes;
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}
function fixedError(error) {
  return Object.freeze({ code: /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(String(error?.code ?? "")) ? error.code : "branding.unavailable", requestId: safeChain(error?.requestId) });
}
function safeChain(value) { return /^[A-Za-z0-9_.:-]{1,160}$/.test(String(value ?? "")) ? String(value) : "-"; }
