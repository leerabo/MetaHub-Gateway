import {
  bindConsoleExperienceInteractions,
  hydrateConsoleAssistantDraft,
  renderConsoleExperience,
} from "../console-experience/renderConsoleExperience.js";
import { WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../lib/workspaceNavigation.js";

export function renderWorkspaceConsoleExperience(state, view, roleCode = "", i18n = null) {
  const renderedView = view === "system-status" ? "overview" : view;
  const roleCopy = workspaceOverviewCopy(roleCode, i18n?.locale);
  return renderConsoleExperience(state, {
    domain: "workspace", view: renderedView,
    domainLabel: roleCopy.label,
    hiddenOverviewSources: roleCopy.hiddenSources,
    collapseEmptyOverview: true,
    resolveCitationNavigation: (sourceId) => {
      const key = sourceId === "deployment-summary" ? "config-summary" : sourceId;
      return Object.hasOwn(WORKSPACE_SOURCE_ROUTE_REGISTRY, key)
        ? WORKSPACE_SOURCE_ROUTE_REGISTRY[key] : null;
    },
  });
}

function workspaceOverviewCopy(roleCode, locale = "zh-CN") {
  const role = String(roleCode ?? "");
  const english = locale === "en-US";
  if (["owner", "admin"].includes(role)) {
    return { label: english ? "Organization" : "组织", hiddenSources: ["deployment", "gateway_health"] };
  }
  if (role === "developer") {
    return { label: english ? "Development" : "开发", hiddenSources: ["deployment", "gateway_health", "role_tasks"] };
  }
  if (role === "viewer") {
    return { label: english ? "My workspace" : "我的空间", hiddenSources: ["deployment", "gateway_health", "role_tasks"] };
  }
  if (role === "finance_admin") return { label: english ? "Finance" : "财务", hiddenSources: ["deployment", "gateway_health"] };
  if (["security_admin", "legal_admin", "auditor"].includes(role)) {
    return { label: english ? "Governance" : "治理", hiddenSources: ["deployment", "gateway_health"] };
  }
  return { label: "Workspace", hiddenSources: [] };
}

export function hydrateWorkspaceConsoleAssistant(root, state) {
  return hydrateConsoleAssistantDraft(root, state);
}

export function bindWorkspaceConsoleExperience(root, handlers) {
  return bindConsoleExperienceInteractions(root, handlers);
}
