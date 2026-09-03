import {
  bindConsoleExperienceInteractions,
  hydrateConsoleAssistantDraft,
  renderConsoleExperience,
} from "../console-experience/renderConsoleExperience.js";

export function renderPlatformConsoleExperience(state, view) {
  return renderConsoleExperience(state, { domain: "platform", view });
}

export function hydratePlatformConsoleAssistant(root, state) {
  return hydrateConsoleAssistantDraft(root, state);
}

export function bindPlatformConsoleExperience(root, handlers) {
  return bindConsoleExperienceInteractions(root, handlers);
}
