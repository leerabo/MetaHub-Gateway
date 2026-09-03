import {
  bindConsoleExperienceInteractions,
  hydrateConsoleAssistantDraft,
  renderConsoleExperience,
} from "../console-experience/renderConsoleExperience.js";

export function renderWorkspaceConsoleExperience(state, view) {
  const renderedView = view === "system-status" ? "overview" : view;
  return renderConsoleExperience(state, { domain: "workspace", view: renderedView });
}

export function hydrateWorkspaceConsoleAssistant(root, state) {
  return hydrateConsoleAssistantDraft(root, state);
}

export function bindWorkspaceConsoleExperience(root, handlers) {
  return bindConsoleExperienceInteractions(root, handlers);
}
