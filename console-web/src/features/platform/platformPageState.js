import { PlatformApiError } from "../../lib/platform.js";
import { renderPlatformGate, renderPlatformWorkspace } from "./renderPlatformPage.js";

const PLATFORM_NAVIGATION_IDS = new Set([
  "operators",
  "workspaces",
  "support",
  "audit",
  "billing",
  "verification",
  "compliance",
  "privacy",
  "releases",
  "providers",
  "channels",
  "customer-success",
  "growth",
]);
const WORKSPACE_READ_PERMISSIONS = new Set([
  "platform.workspace.directory.read",
  "platform.billing.read",
  "platform.customer_success.read",
  "platform.workspace.risk.manage",
]);
const WORKSPACE_FILTER_KEYS = new Set([
  "search",
  "kind",
  "operational_status",
  "region",
  "risk_tag",
  "health_status",
  "health_risk",
]);

export function createPlatformPageController({ root, api } = {}) {
  if (!root || !api) throw new TypeError("platform page requires root and api");
  let generation = 0;
  let request = null;
  const state = {
    loading: true,
    session: null,
    navigation: [],
    operators: null,
    plans: null,
    grants: null,
    audit: null,
    diagnostics: null,
    workspaces: null,
    workspaceFilters: {},
    workspaceCursor: null,
    workspaceCursorHistory: [],
    selectedWorkspaceId: null,
    workspaceDetail: null,
    workspaceHealth: null,
    workspaceEvents: null,
    view: "overview",
    error: null,
    feedback: null,
    pendingAction: null,
  };

  const controller = {
    async mount() {
      bind();
      const ticket = begin();
      try {
        state.session = await api.session({ signal: ticket.signal });
        if (!current(ticket)) return false;
        clearProtectedFacts();
        return reload(ticket);
      } catch (error) {
        if (!current(ticket)) return false;
        if (isPlatformSessionFailure(error)) clearPlatformSession();
        state.loading = false;
        state.error = error instanceof PlatformApiError && [401, 403].includes(error.status)
          ? null
          : safeError(error);
        renderPlatformGate(root, state);
        return false;
      }
    },
    async openSession(totpCode) {
      const ticket = begin();
      scrubSensitive();
      clearPlatformSession();
      state.loading = true;
      state.error = null;
      renderPlatformGate(root, { loading: true });
      try {
        state.session = await api.openSession({ totpCode, signal: ticket.signal });
        if (!current(ticket)) return false;
        state.feedback = "平台身份已验证。";
        return reload(ticket);
      } catch (error) {
        if (!current(ticket)) return false;
        state.loading = false;
        state.error = safeError(error);
        renderPlatformGate(root, state);
        return false;
      }
    },
    async refresh() {
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      return reload(ticket);
    },
    selectView(view) {
      const allowed = new Set((state.navigation ?? []).map((item) => item.id));
      state.view = allowed.has(view) ? view : "overview";
      state.pendingAction = null;
      render();
    },
    async createOperator(accountId, roles) {
      return write("操作员已创建。", () => api.createOperator({ accountId, roles }));
    },
    async createPlan(plan) {
      return write("套餐版本已创建。", (signal) => api.createPlan({ plan, signal }));
    },
    async toggleOperator(input) {
      return write("操作员状态已更新。", () => api.updateOperator(input));
    },
    async createGrant(input) {
      return write("支持申请已提交。", () => api.createGrant(input));
    },
    async approveGrant(grantId) {
      return write("平台审批已记录。", () => api.approveGrant({ grantId }));
    },
    prepareRevoke(grantId) {
      state.pendingAction = { kind: "revoke", id: grantId };
      render();
    },
    cancelConfirmation() {
      state.pendingAction = null;
      render();
    },
    async confirmRevoke(grantId) {
      state.pendingAction = null;
      return write("支持授权已撤销。", () => api.revokeGrant({
        grantId,
        reasonCode: "operator_revoked",
      }));
    },
    async openSupport(grantId) {
      const ticket = begin();
      state.loading = true;
      render();
      try {
        await api.supportSession({ grantId, signal: ticket.signal });
        const diagnostics = await api.diagnostics({ signal: ticket.signal });
        if (!current(ticket)) return false;
        state.diagnostics = diagnostics;
        state.loading = false;
        state.feedback = "诊断会话已打开。";
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    async breakGlass(input) {
      const ticket = begin();
      scrubSensitive();
      state.loading = true;
      render();
      try {
        await api.breakGlass({ ...input, signal: ticket.signal });
        const diagnostics = await api.diagnostics({ signal: ticket.signal });
        if (!current(ticket)) return false;
        state.diagnostics = diagnostics;
        state.loading = false;
        state.feedback = "Break-glass 已启动并进入复核队列。";
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    async filterWorkspaces(input) {
      state.workspaceFilters = normalizeWorkspaceFilters(input);
      state.workspaceCursor = null;
      state.workspaceCursorHistory = [];
      clearWorkspaceDetail();
      return loadWorkspacePage();
    },
    async nextWorkspacePage() {
      const cursor = state.workspaces?.next_cursor;
      if (!cursor) return false;
      state.workspaceCursorHistory.push(state.workspaceCursor);
      state.workspaceCursor = cursor;
      return loadWorkspacePage();
    },
    async previousWorkspacePage() {
      if (state.workspaceCursorHistory.length === 0) return false;
      state.workspaceCursor = state.workspaceCursorHistory.pop() ?? null;
      return loadWorkspacePage();
    },
    async openWorkspace(workspaceId) {
      const ticket = begin();
      state.selectedWorkspaceId = String(workspaceId ?? "");
      state.workspaceDetail = null;
      state.workspaceHealth = null;
      state.workspaceEvents = null;
      state.pendingAction = null;
      state.loading = true;
      state.error = null;
      render();
      try {
        const [detail, health, events] = await Promise.all([
          api.workspace({ workspaceId, signal: ticket.signal }),
          api.workspaceHealth({ workspaceId, signal: ticket.signal }),
          api.workspaceEvents({ workspaceId, signal: ticket.signal }),
        ]);
        if (!current(ticket)) return false;
        state.workspaceDetail = detail;
        state.workspaceHealth = health;
        state.workspaceEvents = events;
        state.loading = false;
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    closeWorkspace() {
      clearWorkspaceDetail();
      state.error = null;
      state.pendingAction = null;
      render();
    },
    prepareWorkspaceLifecycle(kind) {
      const workspace = currentWorkspace();
      if (!workspace || !["suspend", "restore"].includes(kind)) return false;
      state.pendingAction = {
        kind: `workspace-${kind}`,
        id: workspace.workspace_id,
        revision: workspace.operational_revision,
      };
      render();
      return true;
    },
    async confirmWorkspaceLifecycle(reasonCode) {
      const pending = state.pendingAction;
      if (!pending || !/^workspace-(?:suspend|restore)$/u.test(pending.kind)) return false;
      const action = pending.kind.slice("workspace-".length);
      state.pendingAction = null;
      return workspaceWrite(
        action === "suspend" ? "Workspace 已冻结。" : "Workspace 已恢复。",
        (signal) => api[action === "suspend" ? "suspendWorkspace" : "restoreWorkspace"]({
          workspaceId: pending.id,
          revision: pending.revision,
          reasonCode,
          signal,
        }),
      );
    },
    async addWorkspaceRiskTag(tagCode, reasonCode) {
      const workspace = currentWorkspace();
      if (!workspace) return false;
      return workspaceWrite("风险标签已添加。", (signal) => api.addWorkspaceRiskTag({
        workspaceId: workspace.workspace_id,
        revision: workspace.risk_revision,
        tagCode,
        reasonCode,
        signal,
      }));
    },
    async removeWorkspaceRiskTag(tagCode, reasonCode) {
      const workspace = currentWorkspace();
      if (!workspace) return false;
      return workspaceWrite("风险标签已移除。", (signal) => api.removeWorkspaceRiskTag({
        workspaceId: workspace.workspace_id,
        revision: workspace.risk_revision,
        tagCode,
        reasonCode,
        signal,
      }));
    },
    async notifyWorkspaceOwner(templateCode, reasonCode) {
      const workspace = currentWorkspace();
      if (!workspace) return false;
      return workspaceWrite("Owner 通知已进入投递队列。", (signal) => api.notifyWorkspaceOwner({
        workspaceId: workspace.workspace_id,
        revision: workspace.operational_revision,
        templateCode,
        reasonCode,
        signal,
      }));
    },
    destroy() {
      generation += 1;
      request?.abort();
      root.replaceChildren();
    },
  };

  async function reload(ticket) {
    try {
      const navigation = await api.navigation({ signal: ticket.signal });
      if (!current(ticket)) return false;
      const permissions = new Set(state.session?.context?.permissions ?? []);
      const next = {
        navigation: normalizeNavigation(navigation.items),
        operators: null,
        plans: null,
        grants: null,
        audit: null,
        workspaces: null,
        workspaceDetail: null,
        workspaceHealth: null,
        workspaceEvents: null,
      };
      const tasks = [];
      if (permissions.has("platform.operator.manage")) {
        tasks.push(api.operators({ signal: ticket.signal }).then((value) => { next.operators = value; }));
      }
      if (permissions.has("platform.catalog.read") && typeof api.plans === "function") {
        tasks.push(api.plans({ signal: ticket.signal }).then((value) => { next.plans = value; }));
      }
      if (permissions.has("platform.support.request") || permissions.has("platform.support.approve")) {
        tasks.push(api.grants({ signal: ticket.signal }).then((value) => { next.grants = value; }));
      }
      if (permissions.has("platform.audit.read")) {
        tasks.push(api.audit({ signal: ticket.signal }).then((value) => { next.audit = value; }));
      }
      if (hasWorkspaceRead(permissions)) {
        tasks.push(api.workspaces({
          filters: state.workspaceFilters,
          cursor: state.workspaceCursor,
          signal: ticket.signal,
        }).then((value) => { next.workspaces = value; }));
        if (state.selectedWorkspaceId) {
          const workspaceId = state.selectedWorkspaceId;
          tasks.push(api.workspace({ workspaceId, signal: ticket.signal }).then((value) => {
            next.workspaceDetail = value;
          }));
          tasks.push(api.workspaceHealth({ workspaceId, signal: ticket.signal }).then((value) => {
            next.workspaceHealth = value;
          }));
          tasks.push(api.workspaceEvents({ workspaceId, signal: ticket.signal }).then((value) => {
            next.workspaceEvents = value;
          }));
        }
      }
      await Promise.all(tasks);
      if (!current(ticket)) return false;
      Object.assign(state, next);
      if (!state.navigation.some((item) => item.id === state.view)) state.view = "overview";
      state.loading = false;
      state.error = null;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadWorkspacePage() {
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const workspaces = await api.workspaces({
        filters: state.workspaceFilters,
        cursor: state.workspaceCursor,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.workspaces = workspaces;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function write(message, operation) {
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(ticket.signal);
      if (!current(ticket)) return false;
      state.feedback = message;
      return reload(ticket);
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function workspaceWrite(message, operation) {
    const workspaceId = state.selectedWorkspaceId;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(ticket.signal);
      if (!current(ticket)) return false;
      state.feedback = message;
      state.workspaceCursor = null;
      state.workspaceCursorHistory = [];
      const refresh = begin();
      const [workspaces, detail, health, events] = await Promise.all([
        api.workspaces({ filters: state.workspaceFilters, cursor: null, signal: refresh.signal }),
        api.workspace({ workspaceId, signal: refresh.signal }),
        api.workspaceHealth({ workspaceId, signal: refresh.signal }),
        api.workspaceEvents({ workspaceId, signal: refresh.signal }),
      ]);
      if (!current(refresh)) return false;
      state.workspaces = workspaces;
      state.workspaceDetail = detail;
      state.workspaceHealth = health;
      state.workspaceEvents = events;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      const activeTicket = current(ticket) ? ticket : { generation, signal: request.signal };
      return fail(activeTicket, error);
    }
  }

  function fail(ticket, error) {
    if (!current(ticket) || error?.name === "AbortError") return false;
    if (isPlatformSessionFailure(error)) {
      clearPlatformSession();
      state.loading = false;
      state.error = null;
      renderPlatformGate(root, state);
      return false;
    }
    state.loading = false;
    state.error = safeError(error);
    render();
    return false;
  }

  function bind() {
    root.addEventListener("submit", (event) => {
      const form = event.target;
      if (!(form instanceof globalThis.HTMLFormElement)) return;
      event.preventDefault();
      const data = new FormData(form);
      if (form.matches("[data-platform-session-form]")) controller.openSession(data.get("totpCode"));
      if (form.matches("[data-operator-form]")) controller.createOperator(data.get("accountId"), data.getAll("roles"));
      if (form.matches("[data-platform-plan-form]")) {
        controller.createPlan({
          plan_code: data.get("planCode"),
          display_name: data.get("displayName"),
          display_order: Number(data.get("displayOrder")),
          billing_period: data.get("billingPeriod"),
          currency: data.get("currency"),
          unit_price: data.get("unitPrice"),
          trial_days: Number(data.get("trialDays")),
          entitlements: {
            gateway_requests_per_month: Number(data.get("gatewayRequests")),
            workspace_seats: Number(data.get("workspaceSeats")),
            model_aliases: Number(data.get("modelAliases")),
            retention_days: Number(data.get("retentionDays")),
            priority_support: data.get("prioritySupport") === "on",
          },
          publish: data.get("publish") === "on",
        });
      }
      if (form.matches("[data-support-grant-form]")) controller.createGrant({ workspaceId: data.get("workspaceId"), reasonCode: data.get("reasonCode"), durationMinutes: data.get("durationMinutes") });
      if (form.matches("[data-break-glass-form]")) {
        const input = { workspaceId: data.get("workspaceId"), reasonCode: data.get("reasonCode"), totpCode: data.get("totpCode") };
        form.querySelectorAll('input[type="password"], input[name="totpCode"]').forEach((node) => { node.value = ""; });
        controller.breakGlass(input);
      }
      if (form.matches("[data-workspace-filter-form]")) {
        controller.filterWorkspaces(Object.fromEntries(data.entries()));
      }
      if (form.matches("[data-workspace-lifecycle-form]")) {
        controller.confirmWorkspaceLifecycle(data.get("reasonCode"));
      }
      if (form.matches("[data-workspace-risk-form]")) {
        controller.addWorkspaceRiskTag(data.get("tagCode"), data.get("reasonCode"));
      }
      if (form.matches("[data-workspace-notification-form]")) {
        controller.notifyWorkspaceOwner(data.get("templateCode"), data.get("reasonCode"));
      }
    });
    root.addEventListener("click", (event) => {
      const target = event.target.closest("button, [data-platform-view]");
      if (!target) return;
      if (target.dataset.platformView) {
        const selectedView = target.dataset.platformView;
        controller.selectView(selectedView);
        [...root.querySelectorAll("[data-platform-view]")]
          .find((item) => item.dataset.platformView === selectedView)
          ?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      }
      const action = target.dataset.platformAction;
      if (action === "refresh") controller.refresh();
      if (action === "approve-grant") controller.approveGrant(target.dataset.grantId);
      if (action === "open-support") controller.openSupport(target.dataset.grantId);
      if (action === "prepare-revoke") controller.prepareRevoke(target.dataset.grantId);
      if (action === "cancel-confirm") controller.cancelConfirmation();
      if (action === "confirm-revoke") controller.confirmRevoke(target.dataset.grantId);
      if (action === "toggle-operator") controller.toggleOperator({ operatorId: target.dataset.operatorId, revision: Number(target.dataset.revision), status: target.dataset.status, roles: String(target.dataset.roles ?? "").split(",").filter(Boolean) });
      if (action === "open-workspace") controller.openWorkspace(target.dataset.workspaceId);
      if (action === "close-workspace") controller.closeWorkspace();
      if (action === "workspace-next") controller.nextWorkspacePage();
      if (action === "workspace-previous") controller.previousWorkspacePage();
      if (action === "prepare-workspace-lifecycle") controller.prepareWorkspaceLifecycle(target.dataset.lifecycleAction);
      if (action === "remove-workspace-risk") controller.removeWorkspaceRiskTag(target.dataset.tagCode, target.dataset.reasonCode);
    });
  }

  function begin() {
    generation += 1;
    request?.abort();
    request = new AbortController();
    return { generation, signal: request.signal };
  }
  function current(ticket) { return ticket.generation === generation && !ticket.signal.aborted; }
  function render() { state.session ? renderPlatformWorkspace(root, state) : renderPlatformGate(root, state); }
  function scrubSensitive() { root.querySelectorAll('input[type="password"], input[name="totpCode"]').forEach((node) => { node.value = ""; }); }
  function currentWorkspace() { return state.workspaceDetail?.workspace ?? null; }
  function clearWorkspaceDetail() {
    state.selectedWorkspaceId = null;
    state.workspaceDetail = null;
    state.workspaceHealth = null;
    state.workspaceEvents = null;
  }
  function clearProtectedFacts() {
    state.navigation = [];
    state.operators = null;
    state.plans = null;
    state.grants = null;
    state.audit = null;
    state.diagnostics = null;
    state.workspaces = null;
    state.workspaceFilters = {};
    state.workspaceCursor = null;
    state.workspaceCursorHistory = [];
    clearWorkspaceDetail();
    state.pendingAction = null;
    state.view = "overview";
  }
  function clearPlatformSession() {
    state.session = null;
    clearProtectedFacts();
  }
  return controller;
}

function normalizeNavigation(items) {
  const values = Array.isArray(items) ? items.filter((item) => item && typeof item.id === "string") : [];
  const seen = new Set();
  const authorized = values.filter((item) => {
    if (!PLATFORM_NAVIGATION_IDS.has(item.id) || seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
  return [{ id: "overview", label: "概览", href: "/platform.html#overview" }, ...authorized];
}

function normalizeWorkspaceFilters(input) {
  const filters = {};
  for (const [key, value] of Object.entries(input ?? {})) {
    const text = String(value ?? "").trim();
    if (WORKSPACE_FILTER_KEYS.has(key) && text) filters[key] = text;
  }
  return filters;
}

function hasWorkspaceRead(permissions) {
  return [...WORKSPACE_READ_PERMISSIONS].some((permission) => permissions.has(permission));
}

function isPlatformSessionFailure(error) {
  return error instanceof PlatformApiError
    && error.status === 401
    && /^platform\.session_(?:required|invalid|expired|revoked)$/u.test(error.code);
}

function safeError(error) {
  if (error instanceof PlatformApiError) {
    return {
      message: error.message,
      requestId: error.requestId,
      code: error.code,
      status: error.status,
    };
  }
  return { message: "平台请求未完成，请稍后重试。", requestId: "-", code: "platform.request_failed", status: 0 };
}
