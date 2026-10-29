// PAGE-119 状态机：绑定租户、权限和导航所有权，丢弃撤权或切租户后的迟到响应。
import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

const PAGE_ID = "partner-portal";
const LANES = Object.freeze(["profile", "projects", "customers", "commission", "mutation", "export", "delivery"]);

/** 生成伙伴门户读取权限快照，用于动态撤权判断。 */
export function partnerPortalReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PARTNER_PORTAL_READ);
}

/** 计算 PAGE-119 页面所有权，绑定用户、租户、权限和导航代次。 */
export function partnerPortalPageOwner(authContext, consoleShellState) {
  const readIdentity = partnerPortalReadIdentitySnapshot(authContext);
  const tenantId = mirroredTenantId(authContext);
  const permissions = permissionCodesFromAuthContext(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === PAGE_ID)
    : null;
  const required = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  if (
    !readIdentity || !tenantId || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== PAGE_ID
    || navigation?.active_navigation_item_id !== PAGE_ID
    || item?.target_section !== PAGE_ID || item?.visible !== true || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.PARTNER_PORTAL_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(permissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  const capability = (permission) => authorizationIdentitySnapshot(authContext, permission);
  return Object.freeze({
    page: PAGE_ID,
    tenantId,
    userId: String(authContext?.user?.user_id ?? ""),
    readIdentity,
    configureIdentity: capability(PERMISSIONS.PARTNER_PORTAL_CONFIGURE),
    customerIdentity: capability(PERMISSIONS.PARTNER_CUSTOMER_MANAGE),
    brandingIdentity: capability(PERMISSIONS.PARTNER_BRANDING_MANAGE),
    exportIdentity: capability(PERMISSIONS.PARTNER_COMMISSION_EXPORT),
    deliveryIdentity: capability(PERMISSIONS.PARTNER_DELIVERY_DOWNLOAD),
    canConfigure: hasPermission(authContext, PERMISSIONS.PARTNER_PORTAL_CONFIGURE),
    canManageCustomers: hasPermission(authContext, PERMISSIONS.PARTNER_CUSTOMER_MANAGE),
    canManageBranding: hasPermission(authContext, PERMISSIONS.PARTNER_BRANDING_MANAGE),
    canExportCommission: hasPermission(authContext, PERMISSIONS.PARTNER_COMMISSION_EXPORT),
    canDownloadDelivery: hasPermission(authContext, PERMISSIONS.PARTNER_DELIVERY_DOWNLOAD),
    permissionCodes: permissions,
    navigationOwner: JSON.stringify([item.id, item.target_section, required, permissions, revision, fingerprint]),
  });
}

/** 创建伙伴门户请求状态机，丢弃迟到响应并清理依赖数据。 */
export function createPartnerPortalPageState({ clearDom = () => {}, clearBranding = () => {} } = {}) {
  let ownerKey = "";
  let phase = "loading";
  let profile = null;
  let profileError = null;
  let projects = Object.freeze([]);
  let projectsState = "idle";
  let projectsError = null;
  let customers = null;
  let customersState = "idle";
  let customersError = null;
  let commission = null;
  let commissionState = "idle";
  let commissionError = null;
  let customerFilters = defaultCustomerFilters();
  let commissionFilters = defaultCommissionFilters();
  let dialog = closedDialog();
  let mutationState = "idle";
  let mutationKind = "";
  let mutationError = null;
  let feedback = null;
  let download = Object.freeze({ lane: "", state: "idle", error: null, requestId: "" });
  const operations = Object.fromEntries(LANES.map((lane) => [lane, null]));
  const generations = Object.fromEntries(LANES.map((lane) => [lane, 0]));

  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) { clear(); return false; }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    return true;
  }

  function beginProfile(owner, refreshGeneration) {
    if (!owns(owner)) return null;
    phase = profile ? "ready" : "loading";
    profileError = null;
    return begin("profile", owner, { refreshGeneration });
  }

  function acceptProfile(ticket, response, owner) {
    if (!isCurrent("profile", ticket, owner) || !response?.partner) return false;
    profile = response.partner;
    phase = "ready";
    profileError = null;
    applyBranding(profile.brand_primary_color);
    return true;
  }

  function rejectProfile(ticket, error, owner) {
    if (!isCurrent("profile", ticket, owner)) return false;
    profile = null;
    clearDependentData();
    if (error?.code === "partner.not_configured" && Number(error?.status) === 404) {
      phase = "not_configured";
      profileError = null;
    } else {
      phase = "error";
      profileError = fixedError(error);
    }
    return true;
  }

  function beginProjects(owner) {
    if (!owns(owner) || !owner.canManageCustomers || !profile) return null;
    projectsState = projects.length ? "ready" : "loading";
    projectsError = null;
    return begin("projects", owner);
  }

  function acceptProjects(ticket, response, owner) {
    if (!isCurrent("projects", ticket, owner) || !Array.isArray(response?.items)) return false;
    projects = response.items;
    projectsState = "ready";
    projectsError = null;
    return true;
  }

  function rejectProjects(ticket, error, owner) {
    if (!isCurrent("projects", ticket, owner)) return false;
    projects = Object.freeze([]);
    projectsState = "error";
    projectsError = fixedError(error);
    return true;
  }

  function beginCustomers(owner) {
    if (!owns(owner) || !profile) return null;
    customersState = customers ? "ready" : "loading";
    customersError = null;
    return begin("customers", owner, { filters: customerFilters });
  }

  function acceptCustomers(ticket, response, owner) {
    if (!isCurrent("customers", ticket, owner) || !Array.isArray(response?.items)) return false;
    customers = response;
    customersState = "ready";
    customersError = null;
    return true;
  }

  function rejectCustomers(ticket, error, owner) {
    if (!isCurrent("customers", ticket, owner)) return false;
    customersState = "error";
    customersError = fixedError(error);
    return true;
  }

  function beginCommission(owner) {
    if (!owns(owner) || !profile) return null;
    commissionState = commission ? "ready" : "loading";
    commissionError = null;
    return begin("commission", owner, { filters: commissionFilters });
  }

  function acceptCommission(ticket, response, owner) {
    if (!isCurrent("commission", ticket, owner) || !Array.isArray(response?.items)) return false;
    commission = response;
    commissionState = "ready";
    commissionError = null;
    return true;
  }

  function rejectCommission(ticket, error, owner) {
    if (!isCurrent("commission", ticket, owner)) return false;
    commissionState = "error";
    commissionError = fixedError(error);
    return true;
  }

  function setCustomerFilters(owner, next) {
    if (!owns(owner) || !validFilterObject(next, ["start_date", "end_date", "status", "q", "limit", "offset"])) return false;
    customerFilters = Object.freeze({ ...defaultCustomerFilters(), ...next });
    return true;
  }

  function setCommissionFilters(owner, next) {
    if (!owns(owner) || !validFilterObject(next, ["start_date", "end_date", "customer_id", "limit", "offset"])) return false;
    commissionFilters = Object.freeze({ ...defaultCommissionFilters(), ...next });
    return true;
  }

  function openCustomerDialog(owner, kind, customerId = "") {
    if (!owns(owner) || !owner.canManageCustomers || operations.mutation) return false;
    if (kind === "create") {
      dialog = Object.freeze({ open: true, kind, customerId: "", revision: 0, displayName: "" });
    } else {
      const customer = customers?.items?.find((item) => item.partner_customer_id === customerId);
      if (!customer || customer.status !== "active" || !["rename", "disable"].includes(kind)) return false;
      dialog = Object.freeze({
        open: true, kind, customerId, revision: customer.revision, displayName: customer.display_name,
      });
    }
    mutationState = "idle";
    mutationError = null;
    feedback = null;
    return true;
  }

  function closeDialog(owner) {
    if (!owns(owner) || operations.mutation) return false;
    dialog = closedDialog();
    mutationError = null;
    return true;
  }

  function beginMutation(owner, kind, extras = {}) {
    if (!owns(owner) || operations.mutation || !mutationAllowed(owner, kind)) return null;
    mutationState = "submitting";
    mutationKind = kind;
    mutationError = null;
    feedback = null;
    return begin("mutation", owner, { kind, ...extras });
  }

  function acceptPartnerMutation(ticket, response, owner) {
    if (!isCurrent("mutation", ticket, owner) || !response?.partner) return false;
    profile = response.partner;
    phase = "ready";
    mutationState = "saved";
    dialog = closedDialog();
    feedback = fixedFeedback("伙伴门户配置已保存", response.request_id);
    applyBranding(profile.brand_primary_color);
    return true;
  }

  function acceptCustomerMutation(ticket, response, owner) {
    if (!isCurrent("mutation", ticket, owner) || !response?.customer) return false;
    const customer = response.customer;
    const items = customers?.items ?? [];
    const nextItems = [customer, ...items.filter((item) => item.partner_customer_id !== customer.partner_customer_id)];
    if (customers) customers = Object.freeze({ ...customers, items: Object.freeze(nextItems), total: Math.max(customers.total, nextItems.length) });
    mutationState = "saved";
    dialog = closedDialog();
    feedback = fixedFeedback(ticket.kind === "disable" ? "客户映射已终止" : "客户映射已保存", response.request_id);
    return true;
  }

  function rejectMutation(ticket, error, owner) {
    if (!isCurrent("mutation", ticket, owner)) return false;
    mutationState = "error";
    mutationError = fixedError(error);
    feedback = null;
    return true;
  }

  function beginDownload(owner, lane) {
    if (!owns(owner) || !["export", "delivery"].includes(lane)) return null;
    if (lane === "export" && !owner.canExportCommission) return null;
    if (lane === "delivery" && !owner.canDownloadDelivery) return null;
    download = Object.freeze({ lane, state: "loading", error: null, requestId: "" });
    const filters = Object.freeze({
      start_date: commissionFilters.start_date,
      end_date: commissionFilters.end_date,
      customer_id: commissionFilters.customer_id,
    });
    return begin(lane, owner, { partnerCode: profile?.partner_code ?? "", filters });
  }

  function acceptDownload(ticket, result, owner) {
    if (!isCurrent(ticket?.lane, ticket, owner)) return false;
    download = Object.freeze({ lane: ticket.lane, state: "saved", error: null, requestId: safeChain(result?.requestId) });
    return true;
  }

  function rejectDownload(ticket, error, owner) {
    if (!isCurrent(ticket?.lane, ticket, owner)) return false;
    download = Object.freeze({ lane: ticket.lane, state: "error", error: fixedError(error), requestId: "" });
    return true;
  }

  function begin(lane, owner, extras = {}) {
    if (!LANES.includes(lane) || !owns(owner)) return null;
    operations[lane]?.controller.abort();
    generations[lane] += 1;
    const ticket = Object.freeze({
      lane, generation: generations[lane], ownerKey, controller: new AbortController(), ...extras,
    });
    operations[lane] = ticket;
    return ticket;
  }

  function isCurrent(lane, ticket, owner) {
    return Boolean(
      LANES.includes(lane) && ticket && operations[lane] === ticket
      && ticket.generation === generations[lane] && ticket.ownerKey === ownerKey
      && ticket.ownerKey === ownerKeyFor(owner) && !ticket.controller.signal.aborted,
    );
  }

  function finish(ticket) {
    if (ticket && operations[ticket.lane] === ticket) operations[ticket.lane] = null;
  }

  function clear() {
    const pending = LANES.map((lane) => operations[lane]);
    ownerKey = "";
    phase = "loading";
    profile = null;
    profileError = null;
    clearDependentData();
    customerFilters = defaultCustomerFilters();
    commissionFilters = defaultCommissionFilters();
    dialog = closedDialog();
    mutationState = "idle";
    mutationKind = "";
    mutationError = null;
    feedback = null;
    download = Object.freeze({ lane: "", state: "idle", error: null, requestId: "" });
    for (const lane of LANES) { operations[lane] = null; generations[lane] += 1; }
    try {
      clearBranding();
      clearDom();
    } finally {
      pending.forEach((ticket) => ticket?.controller.abort());
    }
  }

  function snapshot() {
    return Object.freeze({
      phase, profile, profileError, projects, projectsState, projectsError,
      customers, customersState, customersError, commission, commissionState, commissionError,
      customerFilters, commissionFilters, dialog, mutationState, mutationKind, mutationError, feedback, download,
    });
  }

  function owns(owner) { const key = ownerKeyFor(owner); return Boolean(key && key === ownerKey); }

  function clearDependentData() {
    projects = Object.freeze([]);
    projectsState = "idle";
    projectsError = null;
    customers = null;
    customersState = "idle";
    customersError = null;
    commission = null;
    commissionState = "idle";
    commissionError = null;
  }

  function applyBranding(color) {
    const root = globalThis.document?.documentElement;
    if (root && /^#[0-9A-F]{6}$/.test(String(color ?? ""))) root.style.setProperty("--partner-brand-primary", color);
  }

  return Object.freeze({
    enter, clear, snapshot, beginProfile, acceptProfile, rejectProfile, beginProjects, acceptProjects,
    rejectProjects, beginCustomers, acceptCustomers, rejectCustomers, beginCommission, acceptCommission,
    rejectCommission, setCustomerFilters, setCommissionFilters, openCustomerDialog, closeDialog,
    beginMutation, acceptPartnerMutation, acceptCustomerMutation, rejectMutation, beginDownload,
    acceptDownload, rejectDownload, isCurrent, finish,
  });
}

/** 判断请求是否由页面生命周期主动取消。 */
export function isPartnerPortalAbortError(error) { return error?.name === "AbortError"; }

function ownerKeyFor(owner) {
  if (!owner || owner.page !== PAGE_ID || !owner.tenantId || !owner.readIdentity || !owner.navigationOwner) return "";
  return JSON.stringify([
    owner.page, owner.tenantId, owner.userId, owner.readIdentity, owner.configureIdentity,
    owner.customerIdentity, owner.brandingIdentity, owner.exportIdentity, owner.deliveryIdentity,
    owner.navigationOwner, owner.permissionCodes,
  ]);
}

function mirroredTenantId(authContext) {
  const user = String(authContext?.user?.tenant_id ?? "").trim();
  const tenant = String(authContext?.tenant?.tenant_id ?? "").trim();
  return user && tenant && user === tenant ? tenant : "";
}

function normalizePermissionCodes(value) {
  if (!Array.isArray(value) || value.some((code) => typeof code !== "string" || !code)) return null;
  const normalized = [...new Set(value)].sort();
  return normalized.length === value.length ? normalized : null;
}

function mutationAllowed(owner, kind) {
  if (kind === "configure") return owner.canConfigure;
  if (kind === "branding") return owner.canManageBranding;
  return ["create", "rename", "disable"].includes(kind) && owner.canManageCustomers;
}

function validFilterObject(value, allowed) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).every((key) => allowed.includes(key)));
}

function defaultCustomerFilters() {
  return Object.freeze({ start_date: "", end_date: "", status: "", q: "", limit: 50, offset: 0 });
}

function defaultCommissionFilters() {
  return Object.freeze({ start_date: "", end_date: "", customer_id: "", limit: 50, offset: 0 });
}

function closedDialog() {
  return Object.freeze({ open: false, kind: "", customerId: "", revision: 0, displayName: "" });
}

function fixedError(error) {
  const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(String(error?.code ?? ""))
    ? String(error.code) : "partner.unavailable";
  return Object.freeze({ code, requestId: safeChain(error?.requestId) || "-" });
}

function fixedFeedback(message, requestId) {
  return Object.freeze({ message, requestId: safeChain(requestId) || "-" });
}

function safeChain(value) {
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(String(value ?? "")) ? String(value) : "";
}
