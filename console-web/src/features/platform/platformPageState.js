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

export function createPlatformPageController({ root, api } = {}) {
  if (!root || !api) throw new TypeError("platform page requires root and api");
  let generation = 0;
  let request = null;
  const state = {
    loading: true,
    session: null,
    navigation: [],
    operators: null,
    grants: null,
    audit: null,
    diagnostics: null,
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
        state.error = error instanceof PlatformApiError && [401, 403].includes(error.status) ? null : safeError(error);
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
      return write("支持授权已撤销。", () => api.revokeGrant({ grantId, reasonCode: "operator_revoked" }));
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
        grants: null,
        audit: null,
      };
      const tasks = [];
      if (permissions.has("platform.operator.manage")) tasks.push(api.operators({ signal: ticket.signal }).then((value) => { next.operators = value; }));
      if (permissions.has("platform.support.request") || permissions.has("platform.support.approve")) tasks.push(api.grants({ signal: ticket.signal }).then((value) => { next.grants = value; }));
      if (permissions.has("platform.audit.read")) tasks.push(api.audit({ signal: ticket.signal }).then((value) => { next.audit = value; }));
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

  async function write(message, operation) {
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation();
      if (!current(ticket)) return false;
      state.feedback = message;
      return reload(ticket);
    } catch (error) {
      return fail(ticket, error);
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
      if (form.matches("[data-support-grant-form]")) controller.createGrant({ workspaceId: data.get("workspaceId"), reasonCode: data.get("reasonCode"), durationMinutes: data.get("durationMinutes") });
      if (form.matches("[data-break-glass-form]")) {
        const input = { workspaceId: data.get("workspaceId"), reasonCode: data.get("reasonCode"), totpCode: data.get("totpCode") };
        form.querySelectorAll('input[type="password"], input[name="totpCode"]').forEach((node) => { node.value = ""; });
        controller.breakGlass(input);
      }
    });
    root.addEventListener("click", (event) => {
      const target = event.target.closest("button, [data-platform-view]");
      if (!target) return;
      if (target.dataset.platformView) controller.selectView(target.dataset.platformView);
      const action = target.dataset.platformAction;
      if (action === "refresh") controller.refresh();
      if (action === "approve-grant") controller.approveGrant(target.dataset.grantId);
      if (action === "open-support") controller.openSupport(target.dataset.grantId);
      if (action === "prepare-revoke") controller.prepareRevoke(target.dataset.grantId);
      if (action === "cancel-confirm") controller.cancelConfirmation();
      if (action === "confirm-revoke") controller.confirmRevoke(target.dataset.grantId);
      if (action === "toggle-operator") controller.toggleOperator({ operatorId: target.dataset.operatorId, revision: Number(target.dataset.revision), status: target.dataset.status, roles: String(target.dataset.roles ?? "").split(",").filter(Boolean) });
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
  function clearProtectedFacts() {
    state.navigation = [];
    state.operators = null;
    state.grants = null;
    state.audit = null;
    state.diagnostics = null;
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

function isPlatformSessionFailure(error) {
  return error instanceof PlatformApiError
    && error.status === 401
    && /^platform\.session_(?:required|invalid|expired|revoked)$/u.test(error.code);
}

function safeError(error) {
  if (error instanceof PlatformApiError) return { message: error.message, requestId: error.requestId };
  return { message: "平台请求未完成，请稍后重试。", requestId: "-" };
}
