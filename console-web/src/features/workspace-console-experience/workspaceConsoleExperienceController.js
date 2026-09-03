import { createWorkspaceConsoleExperienceStore } from "./workspaceConsoleExperienceStore.js";

const VIEW_PERMISSIONS = Object.freeze({
  overview: ["workspace.console.overview.read"],
  "admin-assistant": ["workspace.console.assistant.query"],
  approvals: ["workspace.console.approval.read"],
  "system-status": ["workspace.console.overview.read"],
  "deployment-summary": [
    "workspace.deployment_diagnostic.read",
    "workspace.deployment_diagnostic.audit_read",
  ],
});
const VIEW_NAVIGATION = Object.freeze({
  overview: "overview",
  "admin-assistant": "admin-assistant",
  approvals: "approvals",
  "system-status": "system-status",
  "deployment-summary": "config-summary",
});

export function createWorkspaceConsoleExperienceController({
  api,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
} = {}) {
  if (!api) throw new TypeError("workspace console experience requires api");
  const store = createWorkspaceConsoleExperienceStore();
  let owner = null;

  function enter(authContext, consoleShellState, view) {
    owner = workspaceConsoleExperienceOwner(authContext, consoleShellState, view);
    const accepted = store.enter(owner, view);
    publish();
    return accepted;
  }

  async function load() {
    const view = store.snapshot().view;
    if (view === "admin-assistant") return true;
    const ticket = store.begin(view);
    if (!ticket) return false;
    publish();
    try {
      let value;
      if (view === "overview" || view === "system-status") {
        value = await api.consoleOverview({ signal: ticket.signal });
      }
      else if (view === "approvals") {
        value = await api.consoleApprovals({
          ...ticket.filters,
          cursor: ticket.cursor,
          signal: ticket.signal,
        });
      } else value = await api.deploymentSummary({ signal: ticket.signal });
      const accepted = store.accept(ticket, value, owner);
      publish();
      return accepted;
    } catch (error) {
      return handle(ticket, error);
    }
  }

  function setAssistantDraft(value) {
    const changed = store.setAssistantDraft(owner, value);
    if (changed) publish();
    return changed;
  }

  async function submitAssistant() {
    const ticket = store.begin("assistant");
    if (!ticket) return false;
    publish();
    try {
      const value = await api.consoleAssistant({
        question: ticket.question,
        signal: ticket.signal,
      });
      const accepted = store.accept(ticket, value, owner);
      publish();
      return accepted;
    } catch (error) {
      return handle(ticket, error);
    }
  }

  function clearAssistant() {
    const changed = store.clearAssistant(owner);
    if (changed) publish();
    return changed;
  }

  async function configureApprovals(filters) {
    if (!store.configureApprovals(owner, filters)) return false;
    publish();
    return load();
  }

  async function nextApprovals() {
    if (!store.nextApprovals(owner)) return false;
    publish();
    return load();
  }

  async function previousApprovals() {
    if (!store.previousApprovals(owner)) return false;
    publish();
    return load();
  }

  function leave() {
    owner = null;
    store.leave();
    publish();
  }

  function snapshot() {
    return store.snapshot();
  }

  async function handle(ticket, error) {
    const accepted = store.reject(ticket, error, owner);
    publish();
    if (accepted && error?.status === 401) onSessionInvalid(error);
    else if (accepted && error?.status === 403) onPermissionRevoked(error);
    return false;
  }

  function publish() {
    onChange(store.snapshot());
  }

  return Object.freeze({
    enter,
    load,
    setAssistantDraft,
    submitAssistant,
    clearAssistant,
    configureApprovals,
    nextApprovals,
    previousApprovals,
    leave,
    snapshot,
  });
}

export function workspaceConsoleExperienceOwner(authContext, consoleShellState, view) {
  const requiredPermissions = VIEW_PERMISSIONS[view];
  const navigationId = VIEW_NAVIGATION[view];
  const permissions = Array.isArray(authContext?.permission_codes)
    ? authContext.permission_codes.map(String)
    : [];
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === navigationId)
    : null;
  const accountId = String(authContext?.user?.user_id ?? "");
  const tenantId = String(authContext?.tenant?.tenant_id ?? "");
  const mirroredTenantId = String(authContext?.user?.tenant_id ?? "");
  const sessionId = String(authContext?.session?.session_id ?? "");
  const permissionRevision = Number(authContext?.permission_revision);
  const permissionFingerprint = String(authContext?.authorization_fingerprint ?? "");
  if (!requiredPermissions || !requiredPermissions.some((permission) => permissions.includes(permission))
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== navigationId
    || navigation?.active_navigation_item_id !== navigationId
    || !item || item.visible === false || item.disabled === true
    || !accountId || !tenantId || tenantId !== mirroredTenantId || !sessionId
    || !Number.isSafeInteger(permissionRevision) || permissionRevision < 1
    || !/^[0-9a-f]{64}$/u.test(permissionFingerprint)) return null;
  return Object.freeze({
    accountId,
    tenantId,
    sessionId,
    permissionRevision,
    permissionFingerprint,
    permissions: Object.freeze([...permissions]),
  });
}
