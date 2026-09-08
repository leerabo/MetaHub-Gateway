import {
  bindConsoleExperienceInteractions,
  hydrateConsoleAssistantDraft,
  renderConsoleExperience,
} from "../console-experience/renderConsoleExperience.js";
import { WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../lib/workspaceNavigation.js";

export function renderWorkspaceConsoleExperience(state, view) {
  const renderedView = view === "system-status" ? "overview" : view;
  return renderConsoleExperience(state, {
    domain: "workspace", view: renderedView,
    resolveCitationNavigation: (sourceId) => {
      const key = sourceId === "deployment-summary" ? "config-summary" : sourceId;
      return Object.hasOwn(WORKSPACE_SOURCE_ROUTE_REGISTRY, key)
        ? WORKSPACE_SOURCE_ROUTE_REGISTRY[key] : null;
    },
  });
}

export function hydrateWorkspaceConsoleAssistant(root, state) {
  return hydrateConsoleAssistantDraft(root, state);
}

export function bindWorkspaceConsoleExperience(root, handlers) {
  return bindConsoleExperienceInteractions(root, handlers);
}
