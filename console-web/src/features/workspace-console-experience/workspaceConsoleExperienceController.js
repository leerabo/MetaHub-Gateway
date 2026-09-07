import { createWorkspaceConsoleExperienceStore } from "./workspaceConsoleExperienceStore.js";
import { workspaceNavigationOwner } from "../workspace-navigation/workspaceNavigationStore.js";

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

  function enter(consoleShellState, view) {
    owner = workspaceConsoleExperienceOwner(consoleShellState, view);
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

  function owns(consoleShellState, view) {
    return store.owns(workspaceConsoleExperienceOwner(consoleShellState, view), view);
  }

  async function handle(ticket, error) {
    const failedOwner = owner;
    const accepted = store.reject(ticket, error, owner);
    publish();
    if (accepted && owner === failedOwner) {
      if (error?.status === 401) onSessionInvalid(error);
      else if (error?.status === 403) onPermissionRevoked(error);
    }
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
    owns,
  });
}

export function workspaceConsoleExperienceOwner(consoleShellState, view) {
  const requiredPermissions = VIEW_PERMISSIONS[view];
  const navigationId = VIEW_NAVIGATION[view];
  const authorization = consoleShellState?.workspaceAuthorization;
  const permissions = authorization?.permission_codes;
  const sessionOwner = workspaceNavigationOwner(consoleShellState?.workspaceIdentity, {
    context: consoleShellState?.workspaceContext, authorization,
  });
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === navigationId)
    : null;
  if (!sessionOwner || !Array.isArray(permissions)
    || !permissions.every((permission) => typeof permission === "string")
    || !requiredPermissions || !requiredPermissions.some((permission) => permissions.includes(permission))
    || consoleShellState?.state !== "ok"
    || consoleShellState.workspaceNavigationContract !== true
    || consoleShellState?.activeNavigationItemId !== navigationId
    || navigation?.active_navigation_item_id !== navigationId
    || consoleShellState.activeRouteCode !== navigation.active_route_code
    || item?.route_code !== navigation.active_route_code
    || !item || item.visible === false || item.disabled === true
    || Object.entries(sessionOwner).some(([key, value]) => consoleShellState.workspaceOwner?.[key] !== value)
    || navigation.permission_revision !== sessionOwner.permissionRevision
    || navigation.membership_revision !== sessionOwner.membershipRevision
    || navigation.permission_fingerprint !== sessionOwner.permissionFingerprint
    || !/^ncs_[0-9a-f]{32}$/u.test(navigation.contract_snapshot_id)
    || !Number.isSafeInteger(navigation.contract_generation) || navigation.contract_generation < 1
    || !Number.isSafeInteger(navigation.navigation_epoch) || navigation.navigation_epoch < 1
    || Date.parse(sessionOwner.globalExpiresAt) <= Date.now()
    || Date.parse(sessionOwner.workspaceExpiresAt) <= Date.now()) return null;
  return Object.freeze({
    ...sessionOwner,
    contractSnapshotId: navigation.contract_snapshot_id,
    contractGeneration: navigation.contract_generation,
    navigationEpoch: navigation.navigation_epoch,
    permissions: Object.freeze([...permissions]),
  });
}
